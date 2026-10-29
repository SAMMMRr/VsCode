from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import secrets
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputRichBlockButtons,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichMessage,
    InputRichMessageContent,
    Message,
    RichMessageButton,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TMDB_READ_ACCESS_TOKEN = os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()
TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "ru-RU").strip()
DB_PATH = os.getenv("DB_PATH", "kinoklub.sqlite3")
_media_cache_chat_id = os.getenv("BOT_MEDIA_CACHE_CHAT_ID", "").strip()
BOT_MEDIA_CACHE_CHAT_ID: int | str | None = (
    int(_media_cache_chat_id)
    if _media_cache_chat_id.lstrip("-").isdigit()
    else _media_cache_chat_id or None
)
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kinoklub")
router = Router()
poster_cache_lock = asyncio.Lock()
poster_cache_tasks: dict[str, asyncio.Task[None]] = {}
POSTER_UPLOAD_INTERVAL_SECONDS = 1.1
INLINE_SEARCH_TIMEOUT_SECONDS = 3
INLINE_DETAILS_TIMEOUT_SECONDS = 1
INLINE_PREPARATION_TIMEOUT_SECONDS = 6
POSTER_CACHE_BUILD = "2026-10-29-inline-v3"
poster_last_upload_at = 0.0


@dataclass
class Movie:
    tmdb_id: int
    title: str
    year: str
    overview: str
    poster_path: str | None
    tmdb_rating: float
    media_type: str = "movie"
    genres: tuple[str, ...] = ()
    runtime_minutes: int | None = None

    @classmethod
    def from_tmdb(cls, item: dict[str, Any]) -> "Movie":
        title = item.get("title") or item.get("name") or "Без названия"
        date = item.get("release_date") or item.get("first_air_date") or ""
        episode_runtime = item.get("episode_run_time") or []
        return cls(
            tmdb_id=int(item["id"]),
            title=str(title),
            year=str(date[:4]) if date else "год неизвестен",
            overview=str(item.get("overview") or "Описание пока отсутствует."),
            poster_path=item.get("poster_path"),
            tmdb_rating=float(item.get("vote_average") or 0),
            media_type=str(item.get("media_type") or ("tv" if "first_air_date" in item else "movie")),
            genres=tuple(
                str(genre.get("name") if isinstance(genre, dict) else genre)
                for genre in item.get("genres", [])
                if (
                    isinstance(genre, dict) and genre.get("name")
                ) or isinstance(genre, str)
            ),
            runtime_minutes=(
                int(item["runtime"])
                if item.get("runtime")
                else int(episode_runtime[0])
                if episode_runtime and episode_runtime[0]
                else None
            ),
        )

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Movie":
        return cls(
            tmdb_id=int(row["tmdb_id"]), title=row["title"], year=row["year"],
            overview=row["overview"], poster_path=row["poster_path"],
            tmdb_rating=float(row["tmdb_rating"] or 0),
            media_type=str(row["media_type"] or "movie"),
            genres=tuple(json.loads(row["genres"] or "[]")),
            runtime_minutes=(
                int(row["runtime_minutes"])
                if row["runtime_minutes"] is not None
                else None
            ),
        )


class Store:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS movies (
                    tmdb_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    year TEXT NOT NULL,
                    overview TEXT NOT NULL,
                    poster_path TEXT,
                    tmdb_rating REAL NOT NULL DEFAULT 0,
                    media_type TEXT NOT NULL DEFAULT 'movie',
                    genres TEXT NOT NULL DEFAULT '[]',
                    runtime_minutes INTEGER
                );
                CREATE TABLE IF NOT EXISTS ratings (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    rating REAL NOT NULL CHECK(rating BETWEEN 0 AND 10),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id, tmdb_id),
                    FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    review TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id)
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    chat_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id, tmdb_id),
                    FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id)
                );
                CREATE TABLE IF NOT EXISTS rich_cards (
                    token TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id)
                );
                CREATE TABLE IF NOT EXISTS inline_rich_cards (
                    token TEXT PRIMARY KEY,
                    tmdb_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id)
                );
                CREATE TABLE IF NOT EXISTS inline_rich_card_instances (
                    token TEXT NOT NULL,
                    inline_message_id TEXT NOT NULL,
                    chat_instance TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (token, inline_message_id, chat_instance),
                    FOREIGN KEY (token) REFERENCES inline_rich_cards(token)
                );
                CREATE TABLE IF NOT EXISTS poster_file_cache (
                    poster_path TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_ratings_chat_movie ON ratings(chat_id, tmdb_id);
                CREATE INDEX IF NOT EXISTS idx_reviews_chat_movie ON reviews(chat_id, tmdb_id);
                CREATE INDEX IF NOT EXISTS idx_favorites_chat_user ON favorites(chat_id, user_id);
                CREATE INDEX IF NOT EXISTS idx_rich_cards_chat_message
                    ON rich_cards(chat_id, message_id);
                """
            )
            await db.commit()
            cur = await db.execute("PRAGMA table_info(movies)")
            movie_columns = {str(row[1]) for row in await cur.fetchall()}
            if "media_type" not in movie_columns:
                await db.execute(
                    "ALTER TABLE movies ADD COLUMN media_type TEXT NOT NULL DEFAULT 'movie'"
                )
            if "genres" not in movie_columns:
                await db.execute(
                    "ALTER TABLE movies ADD COLUMN genres TEXT NOT NULL DEFAULT '[]'"
                )
            if "runtime_minutes" not in movie_columns:
                await db.execute("ALTER TABLE movies ADD COLUMN runtime_minutes INTEGER")
            await db.commit()
            cur = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='ratings'"
            )
            ratings_sql = (await cur.fetchone())[0]
            compact_sql = "".join(ratings_sql.lower().split())
            if "check(ratingbetween1and10)" in compact_sql:
                await db.execute("PRAGMA foreign_keys=OFF")
                try:
                    await db.execute("BEGIN")
                    await db.execute("ALTER TABLE ratings RENAME TO ratings_legacy")
                    await db.execute(
                        """CREATE TABLE ratings (
                            chat_id TEXT NOT NULL,
                            user_id INTEGER NOT NULL,
                            tmdb_id INTEGER NOT NULL,
                            rating REAL NOT NULL CHECK(rating BETWEEN 0 AND 10),
                            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (chat_id, user_id, tmdb_id),
                            FOREIGN KEY (tmdb_id) REFERENCES movies(tmdb_id)
                        )"""
                    )
                    await db.execute(
                        """INSERT INTO ratings(chat_id,user_id,tmdb_id,rating,updated_at)
                           SELECT CAST(chat_id AS TEXT),user_id,tmdb_id,rating,updated_at
                           FROM ratings_legacy"""
                    )
                    await db.execute("DROP TABLE ratings_legacy")
                    await db.execute(
                        "CREATE INDEX idx_ratings_chat_movie ON ratings(chat_id, tmdb_id)"
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
                finally:
                    await db.execute("PRAGMA foreign_keys=ON")

    async def save_movie(self, movie: Movie) -> None:
        await self.save_movies([movie])

    async def save_movies(self, movies: list[Movie]) -> None:
        if not movies:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                """INSERT INTO movies(
                       tmdb_id,title,year,overview,poster_path,tmdb_rating,
                       media_type,genres,runtime_minutes
                   )
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tmdb_id) DO UPDATE SET
                   title=excluded.title, year=excluded.year, overview=excluded.overview,
                   poster_path=excluded.poster_path, tmdb_rating=excluded.tmdb_rating,
                   media_type=excluded.media_type, genres=excluded.genres,
                   runtime_minutes=excluded.runtime_minutes""",
                [
                    (
                        movie.tmdb_id, movie.title, movie.year, movie.overview,
                        movie.poster_path, movie.tmdb_rating, movie.media_type,
                        json.dumps(movie.genres, ensure_ascii=False),
                        movie.runtime_minutes,
                    )
                    for movie in movies
                ],
            )
            await db.commit()

    async def get_movie(self, tmdb_id: int) -> Movie | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,))
            row = await cur.fetchone()
            return Movie.from_row(row) if row else None

    async def rate(
        self, chat_id: int | str, user_id: int, tmdb_id: int, value: float
    ) -> tuple[float, int, float | None]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT rating FROM ratings WHERE chat_id=? AND user_id=? AND tmdb_id=?",
                (chat_id, user_id, tmdb_id),
            )
            previous = await cur.fetchone()
            await db.execute(
                """INSERT INTO ratings(chat_id,user_id,tmdb_id,rating) VALUES(?,?,?,?)
                   ON CONFLICT(chat_id,user_id,tmdb_id) DO UPDATE SET
                   rating=excluded.rating, updated_at=CURRENT_TIMESTAMP""",
                (chat_id, user_id, tmdb_id, value),
            )
            cur = await db.execute(
                "SELECT AVG(rating), COUNT(*), SUM(CASE WHEN user_id=? THEN 1 ELSE 0 END) FROM ratings WHERE chat_id=? AND tmdb_id=?",
                (user_id, chat_id, tmdb_id),
            )
            avg, count, _ = await cur.fetchone()
            await db.commit()
            return float(avg or 0), int(count or 0), float(previous[0]) if previous else None

    async def group_score(self, chat_id: int | str, tmdb_id: int) -> tuple[float, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT AVG(rating), COUNT(*) FROM ratings WHERE chat_id=? AND tmdb_id=?",
                (chat_id, tmdb_id),
            )
            avg, count = await cur.fetchone()
            return float(avg or 0), int(count or 0)

    async def top(self, chat_id: int, limit: int = 10) -> list[tuple[Movie, float, int]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT m.*, AVG(r.rating) AS group_avg, COUNT(r.rating) AS votes
                   FROM ratings r JOIN movies m ON m.tmdb_id=r.tmdb_id
                   WHERE r.chat_id=? GROUP BY m.tmdb_id
                   ORDER BY group_avg DESC, votes DESC, m.title COLLATE NOCASE ASC LIMIT ?""",
                (chat_id, limit),
            )
            rows = await cur.fetchall()
            return [(Movie.from_row(row), float(row["group_avg"]), int(row["votes"])) for row in rows]

    async def favorites(
        self, chat_id: int, user_id: int, limit: int = 10
    ) -> list[tuple[Movie, float | None]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT DISTINCT m.*, r.rating AS my_rating
                   FROM movies m
                   LEFT JOIN ratings r
                     ON r.tmdb_id=m.tmdb_id AND r.chat_id=? AND r.user_id=?
                   LEFT JOIN favorites f
                     ON f.tmdb_id=m.tmdb_id AND f.user_id=?
                     AND f.chat_id IN (?, ?)
                   WHERE (r.rating>=8 OR f.tmdb_id IS NOT NULL)
                   ORDER BY COALESCE(r.rating, 0) DESC, m.title COLLATE NOCASE LIMIT ?""",
                (chat_id, user_id, user_id, chat_id, f"inline-user:{user_id}", limit),
            )
            rows = await cur.fetchall()
            return [
                (Movie.from_row(row), float(row["my_rating"]) if row["my_rating"] is not None else None)
                for row in rows
            ]

    async def toggle_favorite(
        self, chat_id: int | str, user_id: int, tmdb_id: int
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM favorites WHERE chat_id=? AND user_id=? AND tmdb_id=?",
                (chat_id, user_id, tmdb_id),
            )
            exists = await cur.fetchone()
            if exists:
                await db.execute(
                    "DELETE FROM favorites WHERE chat_id=? AND user_id=? AND tmdb_id=?",
                    (chat_id, user_id, tmdb_id),
                )
                is_favorite = False
            else:
                await db.execute(
                    "INSERT INTO favorites(chat_id,user_id,tmdb_id) VALUES(?,?,?)",
                    (chat_id, user_id, tmdb_id),
                )
                is_favorite = True
            await db.commit()
            return is_favorite

    async def is_favorite(
        self, chat_id: int | str, user_id: int, tmdb_id: int
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM favorites WHERE chat_id=? AND user_id=? AND tmdb_id=?",
                (chat_id, user_id, tmdb_id),
            )
            return await cur.fetchone() is not None

    async def rating_distribution(
        self, chat_id: int | str, tmdb_id: int
    ) -> list[tuple[float, int]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT rating, COUNT(*) FROM ratings
                   WHERE chat_id=? AND tmdb_id=? GROUP BY rating ORDER BY rating DESC""",
                (chat_id, tmdb_id),
            )
            rows = await cur.fetchall()
            return [(float(row[0]), int(row[1])) for row in rows]

    async def register_rich_card(
        self, token: str, chat_id: int, message_id: int, tmdb_id: int
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO rich_cards(token,chat_id,message_id,tmdb_id) VALUES(?,?,?,?)",
                (token, chat_id, message_id, tmdb_id),
            )
            await db.commit()

    async def get_rich_card_movie_id(
        self, token: str, chat_id: int, message_id: int
    ) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT tmdb_id FROM rich_cards
                   WHERE token=? AND chat_id=? AND message_id=?""",
                (token, chat_id, message_id),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def get_rich_card_token(
        self, chat_id: int, message_id: int, tmdb_id: int
    ) -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT token FROM rich_cards
                   WHERE chat_id=? AND message_id=? AND tmdb_id=?""",
                (chat_id, message_id, tmdb_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise LookupError("Rich card callback token is missing")
            return str(row[0])

    async def register_inline_rich_card(self, token: str, tmdb_id: int) -> None:
        await self.register_inline_rich_cards([(token, tmdb_id)])

    async def register_inline_rich_cards(
        self, cards: list[tuple[str, int]]
    ) -> None:
        if not cards:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT INTO inline_rich_cards(token,tmdb_id) VALUES(?,?)",
                cards,
            )
            await db.commit()

    async def bind_inline_rich_card(
        self, token: str, inline_message_id: str, chat_instance: str
    ) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT tmdb_id FROM inline_rich_cards WHERE token=?",
                (token,),
            )
            row = await cur.fetchone()
            if row is None:
                await db.rollback()
                return None
            await db.execute(
                """INSERT OR IGNORE INTO inline_rich_card_instances(
                       token,inline_message_id,chat_instance
                   ) VALUES(?,?,?)""",
                (token, inline_message_id, chat_instance),
            )
            await db.commit()
            return int(row[0])

    async def get_poster_file_id(self, poster_path: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT file_id FROM poster_file_cache WHERE poster_path=?",
                (poster_path,),
            )
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def get_poster_file_ids(
        self, poster_paths: list[str]
    ) -> dict[str, str]:
        unique_paths = list(dict.fromkeys(poster_paths))
        if not unique_paths:
            return {}
        placeholders = ",".join("?" for _ in unique_paths)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT poster_path,file_id FROM poster_file_cache "
                f"WHERE poster_path IN ({placeholders})",
                unique_paths,
            )
            return {
                str(row[0]): str(row[1])
                for row in await cur.fetchall()
            }

    async def save_poster_file_id(self, poster_path: str, file_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO poster_file_cache(poster_path,file_id) VALUES(?,?)
                   ON CONFLICT(poster_path) DO UPDATE SET
                   file_id=excluded.file_id, created_at=CURRENT_TIMESTAMP""",
                (poster_path, file_id),
            )
            await db.commit()

    async def add_review(self, chat_id: int, user_id: int, username: str, tmdb_id: int, review: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO reviews(chat_id,user_id,username,tmdb_id,review) VALUES(?,?,?,?,?)",
                (chat_id, user_id, username, tmdb_id, review),
            )
            await db.commit()

    async def latest_review(self, chat_id: int, tmdb_id: int) -> tuple[str, str] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT username, review FROM reviews WHERE chat_id=? AND tmdb_id=? ORDER BY id DESC LIMIT 1",
                (chat_id, tmdb_id),
            )
            row = await cur.fetchone()
            return (str(row[0]), str(row[1])) if row else None


store = Store(DB_PATH)


class TMDB:
    def __init__(self, token: str):
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.session is not None
        async with self.session.get(f"{TMDB_API}{path}", params=params or {}) as response:
            if response.status == 401:
                raise RuntimeError("TMDB не принял токен. Проверь TMDB_READ_ACCESS_TOKEN в .env.")
            response.raise_for_status()
            return await response.json()

    async def search_many(self, query: str, limit: int = 10) -> list[Movie]:
        """Search movies and TV series for Telegram inline mode."""
        data = await self.request("/search/multi", {
            "query": query,
            "language": TMDB_LANGUAGE,
            "include_adult": "false",
        })
        results = [
            item for item in data.get("results", [])
            if item.get("media_type") in ("movie", "tv") and item.get("id")
        ]
        movies: list[Movie] = []
        seen: set[tuple[str, int]] = set()
        for item in results:
            key = (str(item.get("media_type")), int(item["id"]))
            if key in seen:
                continue
            seen.add(key)
            movies.append(Movie.from_tmdb(item))
            if len(movies) >= limit:
                break
        return movies

    async def search(self, query: str) -> Movie | None:
        movies = await self.search_many(query, limit=1)
        return movies[0] if movies else None

    async def details_many(self, movies: list[Movie]) -> list[Movie]:
        semaphore = asyncio.Semaphore(5)

        async def load_details(movie: Movie) -> Movie:
            async with semaphore:
                try:
                    return await asyncio.wait_for(self.details(movie), timeout=5)
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                    log.warning(
                        "Could not load TMDB details for movie %s: %s",
                        movie.tmdb_id,
                        exc,
                    )
                    return movie

        return await asyncio.gather(*(load_details(movie) for movie in movies))

    async def details(self, movie: Movie) -> Movie:
        media_type = movie.media_type if movie.media_type in ("movie", "tv") else "movie"
        data = await self.request(
            f"/{media_type}/{movie.tmdb_id}",
            {"language": TMDB_LANGUAGE},
        )
        data["media_type"] = media_type
        return Movie.from_tmdb(data)

    async def alternative_poster_paths(self, movie: Movie) -> list[str]:
        media_type = movie.media_type if movie.media_type in ("movie", "tv") else "movie"
        language = TMDB_LANGUAGE.split("-", 1)[0]
        data = await self.request(
            f"/{media_type}/{movie.tmdb_id}/images",
            {
                "language": TMDB_LANGUAGE,
                "include_image_language": f"{language},en,null",
            },
        )
        language_priority = {language: 0, "en": 1, None: 2}
        posters = [
            item
            for item in data.get("posters", [])
            if isinstance(item, dict)
            and isinstance(item.get("file_path"), str)
            and item["file_path"] != movie.poster_path
        ]
        posters.sort(
            key=lambda item: (
                language_priority.get(item.get("iso_639_1"), 3),
                -float(item.get("vote_average") or 0),
            )
        )
        return [str(item["file_path"]) for item in posters[:5]]

    async def trailer(self, movie: Movie) -> str | None:
        media_type = movie.media_type if movie.media_type in ("movie", "tv") else "movie"
        data = await self.request(
            f"/{media_type}/{movie.tmdb_id}/videos",
            {"language": TMDB_LANGUAGE},
        )
        for video in data.get("results", []):
            key = video.get("key")
            if (
                video.get("site") == "YouTube"
                and video.get("type") == "Trailer"
                and isinstance(key, str)
                and key
            ):
                return f"https://www.youtube.com/watch?v={quote(key, safe='')}"
        return None

    async def similar(self, movie: Movie, limit: int = 5) -> list[Movie]:
        media_type = movie.media_type if movie.media_type in ("movie", "tv") else "movie"
        data = await self.request(
            f"/{media_type}/{movie.tmdb_id}/similar",
            {"language": TMDB_LANGUAGE},
        )
        return [
            Movie.from_tmdb({**item, "media_type": media_type})
            for item in data.get("results", [])[:limit]
            if item.get("id")
        ]

    async def random_popular(self) -> Movie | None:
        page = random.randint(1, 8)
        data = await self.request("/discover/movie", {
            "language": TMDB_LANGUAGE, "sort_by": "popularity.desc", "include_adult": "false",
            "vote_count.gte": 100, "page": page,
        })
        results = data.get("results", [])
        return Movie.from_tmdb(random.choice(results)) if results else None


tmdb = TMDB(TMDB_READ_ACCESS_TOKEN)


def format_rating(value: float) -> str:
    return f"{value:g}"


def star_label(value: float) -> str:
    full_stars = int(value)
    half_star = value % 1 == 0.5
    return "⭐" * full_stars + ("½" if half_star else "")


def star_display(value: float) -> str:
    filled = max(0, min(5, round(value)))
    return "⭐" * filled + "☆" * (5 - filled)


def integer_rating(value: float) -> int:
    return int(value + 0.5)


def movie_runtime_label(movie: Movie) -> str | None:
    if not movie.runtime_minutes:
        return None
    if movie.runtime_minutes >= 60:
        hours, minutes = divmod(movie.runtime_minutes, 60)
        duration = f"{hours} ч" + (f" {minutes} мин" if minutes else "")
    else:
        duration = f"{movie.runtime_minutes} мин"
    if movie.media_type == "tv":
        return f"{duration} / серия"
    return duration


def shorten_movie_overview(overview: str, limit: int = 300) -> str:
    text = " ".join(overview.split()) or "Описание пока отсутствует."
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{(shortened or text[: limit - 1]).rstrip()}…"


def movie_keyboard(
    movie: Movie, *, inline_mode: bool = False, is_photo: bool = True
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⭐",
            callback_data=(
                f"rate:{movie.tmdb_id}:{stars}:"
                f"{'photo' if is_photo else 'text'}"
                if inline_mode
                else f"rate:{movie.tmdb_id}:{stars}"
            ),
        )
        for stars in range(1, 6)
    ]])


def movie_caption(movie: Movie, group_avg: float, group_votes: int) -> str:
    title = html.escape(movie.title)
    year = html.escape(movie.year)
    overview = html.escape(shorten_movie_overview(movie.overview))
    tmdb_score = f"{movie.tmdb_rating:.1f}" if movie.tmdb_rating else "—"
    group_line = (
        f"Оценка группы: <b>{integer_rating(group_avg)}/10</b> "
        f"· {group_votes} голос(ов)"
        if group_votes
        else "Оценка группы: пока нет голосов"
    )
    return (
        f"<b>{title} ({year})</b>\n"
        f"🎞 TMDB: <b>{tmdb_score}/10</b>\n"
        f"{group_line}\n\n"
        f"{overview}\n\n"
        "Оцени фильм от 1 до 5 звёзд кнопкой ниже."
    )[:1024]


def inline_movie_caption(movie: Movie, group_avg: float = 0, group_votes: int = 0) -> str:
    title = html.escape(movie.title)
    year = html.escape(movie.year)
    overview = html.escape(shorten_movie_overview(movie.overview))
    tmdb_score = f"{movie.tmdb_rating:.1f}" if movie.tmdb_rating else "—"
    group_line = (
        f"Оценка этой карточки: <b>{integer_rating(group_avg)}/10</b> "
        f"· {group_votes} голос(ов)"
        if group_votes
        else "⭐ Оценка этой карточки: пока нет голосов"
    )
    return (
        f"🎬 <b>{title} ({year})</b>\n"
        f"🎞 Рейтинг TMDB: <b>{tmdb_score}/10</b>\n\n"
        f"{group_line}\n\n"
        f"{overview}\n\n"
        "Поставьте оценку кнопкой ниже."
    )[:1024]


def rich_movie_message(
    movie: Movie,
    group_avg: float,
    group_votes: int,
    *,
    card_token: str,
    include_poster: bool = True,
    poster_file_id: str | None = None,
    rating_prompt: bool = False,
    trailer_url: str | None = None,
    friend_ratings: list[tuple[float, int]] | None = None,
    similar_movies: list[Movie] | None = None,
) -> InputRichMessage:
    if rating_prompt:
        blocks: list[Any] = []
        if include_poster and movie.poster_path:
            blocks.append(
                InputRichBlockPhoto(
                    photo=InputMediaPhoto(
                        media=poster_file_id or f"{TMDB_IMAGE}{movie.poster_path}",
                        parse_mode=None,
                        show_caption_above_media=None,
                    )
                )
            )
        blocks.extend([
            InputRichBlockSectionHeading(
                text=f"⭐ Оценка фильма «{movie.title}»",
                size=2,
            ),
            InputRichBlockParagraph(text="Выберите оценку от 1 до 10:"),
            *[
                InputRichBlockButtons(buttons=[
                    RichMessageButton(
                        text=str(stars),
                        style="primary",
                        callback_data=f"rich:ratevote:{card_token}:{stars}",
                    )
                    for stars in range(start, start + 5)
                ])
                for start in (1, 6)
            ],
            InputRichBlockButtons(buttons=[
                RichMessageButton(
                    text="↩ К карточке",
                    callback_data=f"rich:show:{card_token}",
                )
            ]),
        ])
        return InputRichMessage(blocks=[
            *blocks,
        ])

    genres_runtime = " · ".join(
        item for item in (
            ", ".join(movie.genres) if movie.genres else None,
            movie_runtime_label(movie),
        )
        if item
    ) or "Жанры и продолжительность не указаны"
    group_rating = (
        f"⭐ Средняя оценка группы: {integer_rating(group_avg)}/10 "
        f"· {group_votes} оценок"
        if group_votes
        else "⭐ Рейтинг группы: пока нет оценок"
    )
    overview = shorten_movie_overview(movie.overview)
    blocks: list[Any] = []
    if include_poster and movie.poster_path:
        blocks.append(
            InputRichBlockPhoto(
                photo=InputMediaPhoto(
                    media=poster_file_id or f"{TMDB_IMAGE}{movie.poster_path}",
                    parse_mode=None,
                    show_caption_above_media=None,
                )
            )
        )
    blocks.extend([
        InputRichBlockSectionHeading(
            text=f"🎬 {movie.title} ({movie.year})",
            size=2,
        ),
        InputRichBlockParagraph(text=genres_runtime),
        InputRichBlockParagraph(
            text=(
                f"⭐ Рейтинг TMDB: {movie.tmdb_rating:.1f}/10\n"
                f"{group_rating}"
            )
        ),
        InputRichBlockParagraph(text=overview),
    ])
    blocks.extend([
        InputRichBlockButtons(buttons=[
            RichMessageButton(
                text="⭐ Оценить",
                style="primary",
                callback_data=f"rich:rate:{card_token}",
            ),
            RichMessageButton(
                text="❤️ В избранное",
                style="success",
                callback_data=f"rich:favorite:{card_token}",
            ),
        ]),
        InputRichBlockButtons(buttons=[
            RichMessageButton(
                text="🎬 Трейлер",
                style="link",
                callback_data=f"rich:trailer:{card_token}",
            ),
            RichMessageButton(
                text="👥 Оценки друзей",
                style="link",
                callback_data=f"rich:friends:{card_token}",
            ),
        ]),
        InputRichBlockButtons(buttons=[
            RichMessageButton(
                text="🔎 Похожие фильмы",
                style="link",
                callback_data=f"rich:similar:{card_token}",
            )
        ]),
    ])
    if trailer_url:
        blocks.extend([
            InputRichBlockSectionHeading(text="🎬 Трейлер", size=3),
            InputRichBlockButtons(buttons=[
                RichMessageButton(text="▶ Смотреть на YouTube", url=trailer_url)
            ]),
        ])
    if friend_ratings is not None:
        blocks.append(InputRichBlockSectionHeading(text="👥 Оценки участников", size=3))
        if friend_ratings:
            avg = sum(rating * votes for rating, votes in friend_ratings) / sum(
                votes for _, votes in friend_ratings
            )
            blocks.append(
                InputRichBlockParagraph(
                    text=f"Средняя: {integer_rating(avg)}/10"
                )
            )
            blocks.extend(
                InputRichBlockParagraph(text=f"{rating:g}/10 — {votes} голос(ов)")
                for rating, votes in friend_ratings
            )
        else:
            blocks.append(InputRichBlockParagraph(text="В этой беседе пока нет оценок."))
    if similar_movies is not None:
        blocks.append(InputRichBlockSectionHeading(text="🔎 Похожие фильмы и сериалы", size=3))
        if similar_movies:
            for similar in similar_movies:
                blocks.append(
                    InputRichBlockButtons(buttons=[
                        RichMessageButton(
                            text=f"{similar.title} ({similar.year})"[:64],
                            url=(
                                f"https://www.themoviedb.org/"
                                f"{similar.media_type}/{similar.tmdb_id}"
                            ),
                        )
                    ])
                )
        else:
            blocks.append(InputRichBlockParagraph(text="TMDB не нашёл похожие фильмы."))
    return InputRichMessage(blocks=blocks)


async def send_rich_message(
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    card_token: str,
    tmdb_id: int,
    message_thread_id: int | None = None,
) -> Message:
    sent_message = await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=rich_message,
        message_thread_id=message_thread_id,
    )
    await store.register_rich_card(
        card_token, chat_id, sent_message.message_id, tmdb_id
    )
    return sent_message


async def telegram_poster_file_ids(
    bot: Bot,
    movies: list[Movie],
    *,
    wait_on_flood: bool = True,
) -> dict[str, str]:
    global poster_last_upload_at

    posters = {
        movie.poster_path: movie
        for movie in movies
        if movie.poster_path
    }
    cached = await store.get_poster_file_ids(list(posters))
    missing = [
        movie
        for path, movie in posters.items()
        if path not in cached
    ]
    if not missing:
        return cached
    if BOT_MEDIA_CACHE_CHAT_ID is None:
        raise RuntimeError(
            "Set BOT_MEDIA_CACHE_CHAT_ID to a private Telegram channel "
            "where the bot can post poster files."
        )

    async with poster_cache_lock:
        cached.update(await store.get_poster_file_ids(list(posters)))
        missing = [
            movie
            for movie in missing
            if movie.poster_path not in cached
        ]
        if not missing:
            return cached

        poster_bytes: dict[str, bytes] = {}
        timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_connect=3)
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": "KinoKlub/1.0 (TMDB poster cache)"},
            trust_env=True,
        ) as session:
            downloads = await asyncio.gather(
                *(download_tmdb_poster(session, movie) for movie in missing),
                return_exceptions=True,
            )
        for movie, downloaded in zip(missing, downloads, strict=True):
            if isinstance(downloaded, bytes):
                assert movie.poster_path is not None
                poster_bytes[movie.poster_path] = downloaded
            else:
                log.warning(
                    "Could not download TMDB poster for movie %s (%s): %s",
                    movie.tmdb_id,
                    type(downloaded).__name__,
                    downloaded,
                )

        loop = asyncio.get_running_loop()

        async def upload_batch(
            batch: list[Movie], images: dict[str, bytes]
        ) -> None:
            nonlocal cached
            global poster_last_upload_at

            uploaded_messages = None
            for attempt in range(3 if wait_on_flood else 1):
                delay = POSTER_UPLOAD_INTERVAL_SECONDS - (
                    loop.time() - poster_last_upload_at
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    if len(batch) == 1:
                        movie = batch[0]
                        assert movie.poster_path is not None
                        image = images.get(movie.poster_path)
                        photo = (
                            BufferedInputFile(
                                image,
                                filename=f"poster_{movie.tmdb_id}.jpg",
                            )
                            if image is not None
                            else f"{TMDB_IMAGE}{movie.poster_path}"
                        )
                        uploaded_messages = [
                            await bot.send_photo(
                                chat_id=BOT_MEDIA_CACHE_CHAT_ID,
                                photo=photo,
                            )
                        ]
                    else:
                        uploaded_messages = await bot.send_media_group(
                            chat_id=BOT_MEDIA_CACHE_CHAT_ID,
                            media=[
                                InputMediaPhoto(
                                    media=(
                                        BufferedInputFile(
                                            images[movie.poster_path],
                                            filename=f"poster_{movie.tmdb_id}.jpg",
                                        )
                                        if movie.poster_path in images
                                        else f"{TMDB_IMAGE}{movie.poster_path}"
                                    )
                                )
                                for movie in batch
                                if movie.poster_path
                            ],
                        )
                    poster_last_upload_at = loop.time()
                    break
                except TelegramRetryAfter as exc:
                    poster_last_upload_at = loop.time()
                    if not wait_on_flood or attempt == 2:
                        raise
                    retry_delay = max(1, exc.retry_after) + 1
                    log.warning(
                        "Poster upload rate-limited; retrying in %s seconds.",
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)

            if uploaded_messages is None or len(uploaded_messages) != len(batch):
                raise RuntimeError("Telegram returned an incomplete poster upload")
            for movie, uploaded in zip(batch, uploaded_messages, strict=True):
                if not uploaded.photo:
                    log.warning(
                        "Telegram returned no photo for TMDB movie %s.",
                        movie.tmdb_id,
                    )
                    continue
                assert movie.poster_path is not None
                file_id = uploaded.photo[-1].file_id
                await store.save_poster_file_id(movie.poster_path, file_id)
                cached[movie.poster_path] = file_id
                log.info("Cached poster for TMDB movie %s.", movie.tmdb_id)

        async def upload_resiliently(
            batch: list[Movie], images: dict[str, bytes]
        ) -> None:
            try:
                await upload_batch(batch, images)
            except TelegramBadRequest as exc:
                if len(batch) == 1:
                    log.warning(
                        "Telegram rejected poster for TMDB movie %s: %s",
                        batch[0].tmdb_id,
                        exc,
                    )
                    return
                log.warning(
                    "Telegram rejected a poster batch; retrying posters individually: %s",
                    exc,
                )
                for movie in batch:
                    try:
                        await upload_batch([movie], images)
                    except TelegramBadRequest as item_exc:
                        log.warning(
                            "Telegram rejected poster for TMDB movie %s: %s",
                            movie.tmdb_id,
                            item_exc,
                        )

        downloaded_movies = [
            movie for movie in missing
            if movie.poster_path in poster_bytes
        ]
        if downloaded_movies:
            await upload_resiliently(downloaded_movies, poster_bytes)

        remote_movies = [
            movie for movie in missing
            if movie.poster_path not in poster_bytes
        ]
        if remote_movies:
            await upload_resiliently(remote_movies, {})
    return cached


async def download_tmdb_poster(
    session: aiohttp.ClientSession, movie: Movie
) -> bytes:
    if not movie.poster_path:
        raise RuntimeError("Movie has no TMDB poster path")

    async def download(path: str) -> bytes:
        async with session.get(f"{TMDB_IMAGE}{path}") as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                raise RuntimeError(
                    f"TMDB returned non-image content for movie {movie.tmdb_id}"
                )
            image_chunks: list[bytes] = []
            image_size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                image_size += len(chunk)
                if image_size > 10 * 1024 * 1024:
                    raise RuntimeError(
                        f"TMDB poster for movie {movie.tmdb_id} exceeds 10 MB"
                    )
                image_chunks.append(chunk)
            if not image_chunks:
                raise RuntimeError(
                    f"TMDB returned an empty poster for movie {movie.tmdb_id}"
                )
            return b"".join(image_chunks)

    try:
        return await download_with_retry(download, movie.poster_path)
    except aiohttp.ClientResponseError as primary_error:
        if primary_error.status not in (403, 404):
            raise
        primary_status = primary_error.status

    try:
        alternatives = await tmdb.alternative_poster_paths(movie)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        raise RuntimeError(
            f"Primary poster path returned HTTP {primary_status}; "
            f"alternate TMDB lookup failed: {exc}"
        ) from exc

    for path in alternatives:
        try:
            image = await download_with_retry(download, path)
        except aiohttp.ClientResponseError as exc:
            if exc.status in (403, 404):
                continue
            raise
        log.info(
            "Using alternate TMDB poster path for movie %s.",
            movie.tmdb_id,
        )
        return image

    raise RuntimeError(
        f"Primary TMDB poster path returned HTTP {primary_status} and no "
        f"alternate poster was available for movie {movie.tmdb_id}"
    )


async def download_with_retry(
    download: Callable[[str], Awaitable[bytes]],
    path: str,
    attempts: int = 3,
) -> bytes:
    for attempt in range(attempts):
        try:
            return await download(path)
        except aiohttp.ClientConnectionError:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(0.25 * (2**attempt))
    raise RuntimeError("Poster download attempts exhausted")


async def telegram_poster_file_id(bot: Bot, movie: Movie) -> str | None:
    if not movie.poster_path:
        return None
    file_ids = await telegram_poster_file_ids(bot, [movie])
    return file_ids.get(movie.poster_path)


async def warm_poster_cache(
    bot: Bot, movies: list[Movie], retry_after: int
) -> None:
    try:
        await asyncio.sleep(retry_after)
        pending = movies
        retry_delays = (5, 20, 60)
        for attempt in range(len(retry_delays) + 1):
            retry_delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            try:
                cached = await telegram_poster_file_ids(bot, pending)
                pending = [
                    movie
                    for movie in pending
                    if movie.poster_path not in cached
                ]
                if not pending:
                    return
                if attempt == len(retry_delays):
                    log.warning(
                        "Could not cache posters for TMDB movies after retries: %s",
                        ", ".join(str(movie.tmdb_id) for movie in pending),
                    )
                    return
            except TelegramRetryAfter as exc:
                if attempt == len(retry_delays):
                    log.warning(
                        "Could not cache poster batch after repeated Telegram rate limits."
                    )
                    return
                retry_delay = max(retry_delay, exc.retry_after + 1)
                log.warning(
                    "Poster cache is rate-limited; retrying in %s seconds.",
                    retry_delay,
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError,
                TelegramAPIError,
            ) as exc:
                if attempt == len(retry_delays):
                    log.warning("Could not cache inline poster batch after retries: %s", exc)
                    return
                log.warning(
                    "Poster cache attempt %s failed; retrying in %s seconds: %s",
                    attempt + 1,
                    retry_delay,
                    exc,
                )
            await asyncio.sleep(retry_delay)
    finally:
        current_task = asyncio.current_task()
        for movie in movies:
            if (
                movie.poster_path
                and poster_cache_tasks.get(movie.poster_path) is current_task
            ):
                poster_cache_tasks.pop(movie.poster_path, None)


def schedule_poster_cache(
    bot: Bot, movies: list[Movie], retry_after: int
) -> asyncio.Task[None] | None:
    pending = [
        movie for movie in movies
        if movie.poster_path and movie.poster_path not in poster_cache_tasks
    ]
    if not pending:
        return next(
            (
                poster_cache_tasks[movie.poster_path]
                for movie in movies
                if movie.poster_path and movie.poster_path in poster_cache_tasks
            ),
            None,
        )
    task = asyncio.create_task(warm_poster_cache(bot, pending, retry_after))
    for movie in pending:
        assert movie.poster_path is not None
        poster_cache_tasks[movie.poster_path] = task
    return task


@router.inline_query()
async def inline_movie_search(query: InlineQuery) -> None:
    """Let users type @botusername <movie title> directly in any Telegram chat."""
    text_query = (query.query or "").strip()
    if len(text_query) < 2:
        await answer_inline_query_safely(query, [], cache_time=3)
        return

    try:
        results = await asyncio.wait_for(
            prepare_inline_movie_results(query, text_query),
            timeout=INLINE_PREPARATION_TIMEOUT_SECONDS,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        log.error(
            "Inline movie card preparation failed (%s): %s",
            type(exc).__name__,
            exc or "no exception message",
        )
        results = []

    await answer_inline_query_safely(query, results, cache_time=1)


async def prepare_inline_movie_results(
    query: InlineQuery, text_query: str
) -> list[InlineQueryResultArticle]:
    movies = await asyncio.wait_for(
        tmdb.search_many(text_query, limit=10),
        timeout=INLINE_SEARCH_TIMEOUT_SECONDS,
    )
    try:
        movies = await asyncio.wait_for(
            tmdb.details_many(movies),
            timeout=INLINE_DETAILS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.info("Inline TMDB details timed out; using search results.")

    await store.save_movies(movies)
    card_tokens = [secrets.token_urlsafe(8) for _ in range(len(movies))]
    await store.register_inline_rich_cards([
        (token, movie.tmdb_id)
        for token, movie in zip(card_tokens, movies, strict=True)
    ])

    poster_paths = [
        movie.poster_path for movie in movies if movie.poster_path
    ]
    poster_file_ids = await store.get_poster_file_ids(poster_paths)
    uncached_movies = [
        movie for movie in movies
        if movie.poster_path and movie.poster_path not in poster_file_ids
    ]
    cache_task = schedule_poster_cache(query.bot, uncached_movies, 0)
    if cache_task is not None:
        poster_file_ids = await store.get_poster_file_ids(poster_paths)

    results: list[InlineQueryResultArticle] = []
    for movie, card_token in zip(movies, card_tokens, strict=True):
        poster_file_id = poster_file_ids.get(movie.poster_path or "")
        score = f"{movie.tmdb_rating:.1f}/10" if movie.tmdb_rating else "нет оценки"
        overview = movie.overview.strip() or "Описание пока отсутствует."
        if len(overview) > 130:
            overview = overview[:127].rsplit(" ", 1)[0] + "…"
        results.append(InlineQueryResultArticle(
            id=f"{movie.media_type}_{movie.tmdb_id}_{len(results)}",
            title=f"{movie.title} ({movie.year})",
            description=f"Год выпуска: {movie.year} · TMDB {score} · {overview}",
            thumbnail_url=(
                f"{TMDB_IMAGE}{movie.poster_path}"
                if movie.poster_path
                else None
            ),
            input_message_content=InputRichMessageContent(
                rich_message=rich_movie_message(
                    movie,
                    0,
                    0,
                    card_token=card_token,
                    poster_file_id=poster_file_id,
                    include_poster=poster_file_id is not None,
                )
            ),
        ))
    return results


async def answer_inline_query_safely(
    query: InlineQuery,
    results: list[InlineQueryResultArticle],
    *,
    cache_time: int,
) -> bool:
    try:
        await query.answer(
            results=results,
            cache_time=cache_time,
            is_personal=True,
        )
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        if "query is too old" in error or "query id is invalid" in error:
            log.warning("Telegram inline query expired before it could be answered.")
            return False
        raise
    return True


async def publish_movie(message: Message, movie: Movie) -> None:
    try:
        movie = await tmdb.details(movie)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.warning("TMDB movie details request failed; using search result.")
    chat_id = message.chat.id
    await store.save_movie(movie)
    avg, count = await store.group_score(chat_id, movie.tmdb_id)
    card_token = secrets.token_urlsafe(8)
    await send_rich_message(
        message.bot,
        chat_id,
        rich_movie_message(
            movie, avg, count, card_token=card_token
        ),
        card_token,
        movie.tmdb_id,
        message.message_thread_id,
    )


async def update_rich_movie_card(
    callback: CallbackQuery,
    movie: Movie,
    *,
    card_token: str,
    rating_scope: int | str,
    trailer_url: str | None = None,
    friend_ratings: list[tuple[float, int]] | None = None,
    similar_movies: list[Movie] | None = None,
    rating_prompt: bool = False,
) -> None:
    avg, votes = await store.group_score(rating_scope, movie.tmdb_id)
    poster_file_id = None
    if callback.inline_message_id and movie.poster_path:
        poster_file_id = await store.get_poster_file_id(movie.poster_path)
        if poster_file_id is None:
            poster_file_id = await telegram_poster_file_id(callback.bot, movie)
    rich_message = rich_movie_message(
        movie,
        avg,
        votes,
        card_token=card_token,
        poster_file_id=poster_file_id,
        rating_prompt=rating_prompt,
        trailer_url=trailer_url,
        friend_ratings=friend_ratings,
        similar_movies=similar_movies,
    )
    if callback.inline_message_id:
        await callback.bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            rich_message=rich_message,
        )
    elif callback.message:
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            rich_message=rich_message,
        )
    else:
        raise ValueError("Rich movie card callback has no editable message")


@router.callback_query(F.data.startswith("rich:"))
async def rich_movie_action(callback: CallbackQuery) -> None:
    callback_answered = False

    async def answer_once(text: str, *, show_alert: bool = False) -> None:
        nonlocal callback_answered
        if callback_answered:
            return
        await callback.answer(text, show_alert=show_alert)
        callback_answered = True

    message = callback.message
    is_inline = callback.inline_message_id is not None
    if not is_inline and (
        message is None
        or message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
            ChatType.PRIVATE,
        )
    ):
        await answer_once(
            "Откройте карточку фильма командой /film в чате.",
            show_alert=True,
        )
        return
    if is_inline and not callback.chat_instance:
        await answer_once("Не удалось определить беседу для карточки.", show_alert=True)
        return
    try:
        parts = (callback.data or "").split(":")
        if len(parts) < 3 or parts[0] != "rich":
            raise ValueError("invalid rich callback")
        action = parts[1]
        card_token = parts[2]
        value: int | None = None
        if not 8 <= len(card_token) <= 16 or not card_token.replace("_", "").replace("-", "").isalnum():
            raise ValueError("invalid card token")
        if action == "ratevote":
            if len(parts) != 4:
                raise ValueError("invalid rating callback")
            value = int(parts[3])
            if not 1 <= value <= 10:
                raise ValueError("rating out of range")
        elif action not in {"rate", "favorite", "trailer", "friends", "similar", "show"} or len(parts) != 3:
            raise ValueError("unsupported rich action")
    except ValueError:
        await answer_once("Некорректная кнопка.", show_alert=True)
        return

    if is_inline:
        inline_message_id = callback.inline_message_id
        chat_instance = callback.chat_instance
        assert inline_message_id is not None and chat_instance is not None
        tmdb_id = await store.bind_inline_rich_card(
            card_token,
            inline_message_id,
            chat_instance,
        )
        rating_scope: int | str = f"inline:{chat_instance}"
        favorite_scope: int | str = f"inline-user:{callback.from_user.id}"
    else:
        assert message is not None
        tmdb_id = await store.get_rich_card_movie_id(
            card_token, message.chat.id, message.message_id
        )
        rating_scope = message.chat.id
        favorite_scope = message.chat.id
    if tmdb_id is None:
        await answer_once(
            "Карточка устарела. Найдите фильм командой /film.",
            show_alert=True,
        )
        return
    movie = await store.get_movie(tmdb_id)
    if movie is None:
        await answer_once(
            "Фильм больше не найден в базе. Найдите его командой /film.",
            show_alert=True,
        )
        return

    if is_inline and movie.poster_path:
        if await store.get_poster_file_id(movie.poster_path) is None:
            await answer_once("Загружаю постер и обновляю карточку…")

    try:
        if action == "rate":
            await update_rich_movie_card(
                callback,
                movie,
                card_token=card_token,
                rating_scope=rating_scope,
                rating_prompt=True,
            )
            await answer_once("Выберите оценку от 1 до 10.")
        elif action == "ratevote":
            assert value is not None
            await store.rate(
                rating_scope, callback.from_user.id, tmdb_id, float(value)
            )
            await update_rich_movie_card(
                callback, movie, card_token=card_token, rating_scope=rating_scope
            )
            await answer_once(f"Оценка {value}/10 сохранена.")
        elif action == "favorite":
            is_favorite = await store.toggle_favorite(
                favorite_scope, callback.from_user.id, tmdb_id
            )
            await update_rich_movie_card(
                callback, movie, card_token=card_token, rating_scope=rating_scope
            )
            await answer_once(
                "Добавлено в избранное." if is_favorite else "Убрано из избранного."
            )
        elif action == "trailer":
            trailer_url = await tmdb.trailer(movie)
            if trailer_url is None:
                await answer_once(
                    "Для этого фильма трейлер не найден.",
                    show_alert=True,
                )
                return
            await update_rich_movie_card(
                callback,
                movie,
                card_token=card_token,
                rating_scope=rating_scope,
                trailer_url=trailer_url,
            )
            await answer_once("Нашёл трейлер.")
        elif action == "friends":
            ratings = await store.rating_distribution(rating_scope, tmdb_id)
            await update_rich_movie_card(
                callback,
                movie,
                card_token=card_token,
                rating_scope=rating_scope,
                friend_ratings=ratings,
            )
            await answer_once("Показал оценки участников беседы.")
        elif action == "similar":
            similar_movies = await tmdb.similar(movie)
            await update_rich_movie_card(
                callback,
                movie,
                card_token=card_token,
                rating_scope=rating_scope,
                similar_movies=similar_movies,
            )
            await answer_once("Показал похожие фильмы.")
        else:
            await update_rich_movie_card(
                callback, movie, card_token=card_token, rating_scope=rating_scope
            )
            await answer_once("Вернул карточку фильма.")
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        log.warning("TMDB rich card action failed: %s", exc)
        await answer_once(
            "Не удалось получить данные TMDB. Попробуйте позже.",
            show_alert=True,
        )
    except TelegramBadRequest as exc:
        log.warning("Telegram could not update a rich movie card: %s", exc)
        if action == "ratevote":
            error_message = "Оценка сохранена, но Telegram не смог обновить inline-карточку."
        elif action == "favorite":
            error_message = "Избранное изменено, но Telegram не смог обновить inline-карточку."
        else:
            error_message = "Telegram не смог обновить карточку. Попробуйте ещё раз."
        await answer_once(
            error_message,
            show_alert=True,
        )
    except TelegramAPIError as exc:
        log.error("Telegram API failed while handling a rich movie card: %s", exc)
        await answer_once(
            "Telegram не выполнил действие. Попробуйте ещё раз.",
            show_alert=True,
        )
    except aiosqlite.Error as exc:
        log.error("SQLite failed while handling a rich movie card: %s", exc)
        await answer_once(
            "Не удалось сохранить данные. Попробуйте позже.",
            show_alert=True,
        )
    except LookupError as exc:
        log.error("Rich movie card registry is inconsistent: %s", exc)
        await answer_once(
            "Карточка устарела. Отправьте её заново командой /film.",
            show_alert=True,
        )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "🎬 <b>КиноКлуб</b> — ищем фильмы, ставим оценки и выбираем, что смотреть, прямо в этой беседе.\n\n"
        "Добавьте меня в группу и используйте команды:\n"
        "/film Семь — найти фильм или сериал\n"
        "/random — случайный популярный фильм\n"
        "/top — рейтинг вашей компании\n"
        "/favorites — ваши оценки 8–10 в этой группе\n"
        "/vote Интерстеллар | Семь | Начало — голосование\n"
        "/review Название фильма | текст рецензии\n"
        "/help — все команды\n\n"
        "Оценки хранятся отдельно для каждой группы.\n\n"
        "🔎 Быстрый поиск: в любом чате введи @blastmovie_bot и название фильма, затем выбери результат.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "<b>Команды КиноКлуба</b>\n\n"
        "🔎 <code>/film Семь</code> — карточка фильма/сериала с постером и оценками.\n"
        "🎲 <code>/random</code> — случайный популярный фильм.\n"
        "🏆 <code>/top</code> — топ по оценкам участников этой группы.\n"
        "❤️ <code>/favorites</code> — ваши фильмы с оценкой 8–10 в текущей группе.\n"
        "🗳 <code>/vote Название 1 | Название 2 | Название 3</code> — голосование Telegram.\n"
        "📝 <code>/review Название | Текст отзыва</code> — сохранить и опубликовать рецензию.\n\n"
        "Карточки /film, /random и результаты inline-поиска содержат встроенные кнопки: "
        "оценить, добавить в избранное, открыть трейлер, посмотреть оценки беседы и похожие фильмы.\n\n"
        "Также можно искать прямо в любом чате: напиши @blastmovie_bot и название фильма. "
        "Выбери результат, чтобы отправить карточку в беседу.\n\n"
        "Поиск в строке Telegram: введи @blastmovie_bot и название фильма, не отправляя запрос отдельным сообщением.\n\n"
        "Любой участник может менять свою оценку повторным нажатием."
    )


@router.channel_post(Command("chatid"))
async def channel_chat_id(message: Message) -> None:
    await message.answer(f"ID этого канала: <code>{message.chat.id}</code>")


@router.message(Command("film", "find"))
async def find_movie(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напиши название после команды: <code>/film Семь</code>")
        return
    status_message = await message.answer("🔎 Ищу фильм в TMDB…")
    keep_status_message = False
    try:
        movie = await tmdb.search(query)
        if not movie:
            await status_message.edit_text(
                "Ничего не нашёл. Попробуй оригинальное название или другой вариант написания."
            )
            keep_status_message = True
            return
        await publish_movie(message, movie)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        log.warning("TMDB search failed: %s", exc)
        await status_message.edit_text(
            f"Не получилось найти фильм в TMDB: {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
        keep_status_message = True
    finally:
        if not keep_status_message:
            try:
                await status_message.delete()
            except TelegramAPIError as exc:
                log.info("Could not remove completed film search status: %s", exc)


@router.message(Command("random"))
async def random_movie(message: Message) -> None:
    try:
        movie = await tmdb.random_popular()
        if not movie:
            await message.answer("Пока не удалось подобрать фильм. Попробуй ещё раз.")
            return
        await publish_movie(message, movie)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        log.warning("TMDB random failed: %s", exc)
        await message.answer(f"Не получилось получить рекомендацию: {html.escape(str(exc))}")


@router.callback_query(F.data.startswith("rate:"))
async def rate_movie(callback: CallbackQuery) -> None:
    inline_message_id = callback.inline_message_id
    message = callback.message
    if not inline_message_id and (
        not message
        or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE)
    ):
        await callback.answer("Чтобы оценить фильм, найди его командой /film в чате.", show_alert=True)
        return
    try:
        parts = (callback.data or "").split(":")
        if len(parts) not in (3, 4):
            raise ValueError("invalid callback data")
        _, movie_id_str, rating_str = parts[:3]
        tmdb_id, stars = int(movie_id_str), int(rating_str)
        if (
            not 1 <= stars <= 5
            or (len(parts) == 4 and parts[3] not in ("photo", "text"))
        ):
            raise ValueError("rating out of range")
    except ValueError:
        await callback.answer("Некорректная оценка.", show_alert=True)
        return

    movie = await store.get_movie(tmdb_id)
    if not movie:
        await callback.answer("Карточка устарела. Найди фильм командой /film.", show_alert=True)
        return
    value = stars * 2
    if inline_message_id:
        rating_scope: int | str = f"inline:{inline_message_id}"
    else:
        if message is None:
            await callback.answer("Не удалось определить чат для оценки.", show_alert=True)
            return
        rating_scope = message.chat.id
    _, _, previous = await store.rate(rating_scope, callback.from_user.id, tmdb_id, value)
    avg, count = await store.group_score(rating_scope, tmdb_id)
    await callback.answer(
        f"Оценка {star_label(stars)} сохранена"
        + (f" (раньше {star_label(previous / 2)})" if previous is not None else "")
    )
    caption = (
        inline_movie_caption(movie, avg, count)
        if inline_message_id
        else movie_caption(movie, avg, count)
    )
    try:
        if inline_message_id:
            is_photo = len(parts) == 4 and parts[3] == "photo"
            if is_photo:
                await callback.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption=caption,
                    reply_markup=movie_keyboard(movie, inline_mode=True, is_photo=True),
                    parse_mode=ParseMode.HTML,
                )
            else:
                await callback.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=caption,
                    reply_markup=movie_keyboard(movie, inline_mode=True, is_photo=False),
                    parse_mode=ParseMode.HTML,
                )
        elif message and message.photo:
            await message.edit_caption(
                caption=caption,
                reply_markup=movie_keyboard(movie),
                parse_mode=ParseMode.HTML,
            )
        else:
            if message:
                await message.edit_text(
                    caption, reply_markup=movie_keyboard(movie), parse_mode=ParseMode.HTML
                )
    except TelegramBadRequest as exc:
        log.info("Could not refresh movie card: %s", exc)


@router.message(Command("top"))
async def top_movies(message: Message) -> None:
    rows = await store.top(message.chat.id)
    if not rows:
        await message.answer("В рейтинге пока пусто. Найдите фильм через /film и поставьте первую оценку!")
        return
    lines = ["🏆 <b>Топ вашей компании</b>"]
    for rank, (movie, avg, votes) in enumerate(rows, 1):
        lines.append(
            f"{rank}. <b>{html.escape(movie.title)} ({html.escape(movie.year)})</b> — "
            f"{integer_rating(avg)}/10 · {votes} голос(ов)"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("favorites"))
async def favorites(message: Message) -> None:
    rows = await store.favorites(message.chat.id, message.from_user.id)
    if not rows:
        await message.answer(
            "В избранном пока пусто. Добавьте фильм кнопкой «❤️ В избранное» "
            "или поставьте ему оценку от 8/10."
        )
        return
    lines = ["❤️ <b>Ваши любимые в этой группе</b>"]
    for movie, rating in rows:
        rating_text = (
            f" — {rating:g}/10 {star_display(rating / 2)}"
            if rating is not None
            else ""
        )
        lines.append(
            f"• <b>{html.escape(movie.title)} ({html.escape(movie.year)})</b>"
            f"{rating_text}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("vote"))
async def vote(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    options = [x.strip() for x in raw.split("|") if x.strip()]
    if not 2 <= len(options) <= 10:
        await message.answer("Укажи от 2 до 10 вариантов через вертикальную черту:\n<code>/vote Интерстеллар | Семь | Начало</code>")
        return
    question = "Что смотрим?"
    if len(question) > 300:
        question = question[:300]
    await message.answer_poll(question=question, options=options, is_anonymous=False, allows_multiple_answers=False)


@router.message(Command("review"))
async def review(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if "|" not in raw:
        await message.answer("Формат: <code>/review Семь | Сильная атмосфера и отличный финал</code>")
        return
    title, review_text = [part.strip() for part in raw.split("|", 1)]
    if len(title) < 1 or len(review_text) < 3:
        await message.answer("Добавь название фильма и текст рецензии (минимум 3 символа).")
        return
    try:
        movie = await tmdb.search(title)
        if not movie:
            await message.answer("Не нашёл этот фильм. Сначала попробуй команду /film с его названием.")
            return
        await store.save_movie(movie)
        user = message.from_user
        username = user.full_name or user.username or str(user.id)
        await store.add_review(message.chat.id, user.id, username, movie.tmdb_id, review_text[:1500])
        await message.answer(
            f"📝 <b>Рецензия на «{html.escape(movie.title)}»</b>\n"
            f"Автор: {html.escape(username)}\n\n{html.escape(review_text[:1500])}",
            parse_mode=ParseMode.HTML,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        log.warning("TMDB review lookup failed: %s", exc)
        await message.answer("Не удалось найти фильм для рецензии. Проверь название и попробуй ещё раз.")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и добавь токен от @BotFather.")
    if not TMDB_READ_ACCESS_TOKEN:
        raise SystemExit("Не задан TMDB_READ_ACCESS_TOKEN. Получи бесплатный API Read Access Token в настройках TMDB.")

    await store.init()
    await tmdb.open()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="film", description="Найти фильм или сериал"),
        BotCommand(command="random", description="Случайный популярный фильм"),
        BotCommand(command="top", description="Топ вашей группы"),
        BotCommand(command="favorites", description="Мои любимые в этой группе"),
        BotCommand(command="vote", description="Голосование за вечерний просмотр"),
        BotCommand(command="review", description="Написать рецензию"),
        BotCommand(command="help", description="Список команд"),
    ])
    try:
        log.info(
            "КиноКлуб запущен. Ожидаю сообщения Telegram… "
            "(poster cache %s)",
            POSTER_CACHE_BUILD,
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await tmdb.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
