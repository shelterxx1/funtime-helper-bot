import logging
import os
import re
import secrets
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import telebot


logger = logging.getLogger("telegram_bot")
DATABASE_PATH = Path(__file__).with_name("funtime_users.sqlite3")
MIN_FUNTIME_NICKNAME_LENGTH = 3
MAX_FUNTIME_NICKNAME_LENGTH = 16
CONTACT_PLATFORMS = {
    "telegram": "Telegram",
    "discord": "Discord",
}
PROFILE_BUTTON_TEXT = "👤 Мой профиль"
FIND_BUTTON_TEXT = "🔍 Найти тимейта"
REGISTER_BUTTON_TEXT = "⚙️ Регистрация заново"
MAIN_MENU_TEXT = (
    "🎉 Регистрация завершена! Вы в Главном меню.\n\n"
    "Доступные команды:\n"
    "🔹 /reg — Пройти регистрацию заново\n"
    "🔹 /menu — Открыть главное меню\n"
    "🔹 /profile — Посмотреть свой профиль\n"
    "🔹 /find — Найти тимейта"
)
FIND_MENU_TEXT = "🔍 Найти тимейта\n\nВыберите действие:"
MAX_TEAMMATE_DESCRIPTION_LENGTH = 500
TEAMMATE_MODES = {
    "anarchy": "⚔️ Анархия",
    "light": "⚡ Лайт Анархия",
    "mega": "💥 Mega Анархия",
    "other": "🎮 Другой режим",
}
PARTY_SIZE_CHOICES = {
    "1": "+1",
    "2": "+2",
    "3": "+3",
    "4plus": "+4+",
    "other": "Другое",
}
TEAMMATE_PRIVILEGES = {
    "player": "Игрок",
    "baron": "Барон",
    "guard": "Страж",
    "hero": "Герой",
    "aspid": "Аспид",
    "squid": "Сквид",
    "leader": "Глава",
    "elite": "Элита",
    "titan": "Титан",
    "prince": "Принц",
    "prince2": "Князь",
    "duke": "Герцог",
    "other": "Неопределенная",
}


def validate_funtime_nickname(nickname: str) -> str | None:
    if not MIN_FUNTIME_NICKNAME_LENGTH <= len(nickname) <= MAX_FUNTIME_NICKNAME_LENGTH:
        return "Ник на Funtime должен содержать от 3 до 16 символов."
    if re.fullmatch(r"[A-Za-z0-9_]+", nickname) is None:
        return (
            "Разрешены только английские буквы, цифры и подчёркивание (_). "
            "Кириллица, пробелы, дефисы, точки и спецсимволы запрещены."
        )
    return None


def validate_discord_username(username: str) -> str | None:
    if len(username) < 2:
        return "Discord-юзернейм должен содержать минимум 2 символа."
    if re.fullmatch(r"[a-z0-9._]+", username) is None:
        return (
            "Для Discord разрешены латинские буквы, цифры, точки и подчёркивания. "
            "Другие символы запрещены."
        )
    if ".." in username:
        return "В Discord-юзернейме нельзя ставить две точки подряд."
    if username.startswith(".") or username.endswith("."):
        return "Discord-юзернейм не может начинаться или заканчиваться точкой."
    return None


def validate_telegram_username(username: str) -> str | None:
    if not 5 <= len(username) <= 32:
        return "Telegram-юзернейм должен содержать от 5 до 32 символов."
    if username[0].isdigit():
        return "Telegram-юзернейм не может начинаться с цифры."
    if re.fullmatch(r"[a-z_][a-z0-9_]*", username) is None:
        return (
            "Для Telegram разрешены латинские буквы, цифры и подчёркивание. "
            "Точки, дефисы и другие символы запрещены."
        )
    return None


def normalize_contact_username(platform: str, username: str) -> str:
    normalized = username.strip()
    if platform == "telegram" and normalized.startswith("@"):
        normalized = normalized[1:]
    return normalized.lower()


def contact_username_prompt(platform: str) -> str:
    if platform == "telegram":
        return "Введите ваш юзернейм в Telegram (символ @ вводить не обязательно)."
    return "Введите ваш юзернейм в Discord."


@dataclass
class PendingRegistration:
    session_id: str
    nickname: str | None = None
    contact_platform: str | None = None


@dataclass
class PendingTeammateProfile:
    session_id: str
    editing: bool = False
    stage: str = "mode"
    mode: str | None = None
    party_size: str | None = None
    privilege: str | None = None
    looking_for: str | None = None


@dataclass
class TeammateViewSession:
    session_id: str
    profile_ids: list[int]
    index: int = 0


def build_contact_keyboard(session_id: str) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "Telegram",
            callback_data=f"reg_contact:telegram:{session_id}",
        ),
        telebot.types.InlineKeyboardButton(
            "Discord",
            callback_data=f"reg_contact:discord:{session_id}",
        ),
    )
    return keyboard


def build_teammate_menu_keyboard(
    has_profile: bool,
) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
    if has_profile:
        keyboard.add(
            telebot.types.InlineKeyboardButton(
                "👀 Смотреть анкеты",
                callback_data="team_action:view",
            ),
            telebot.types.InlineKeyboardButton(
                "✏️ Редактировать анкету",
                callback_data="team_action:edit",
            ),
            telebot.types.InlineKeyboardButton(
                "❌ Удалить анкету",
                callback_data="team_action:delete",
            )
        )
    else:
        keyboard.add(
            telebot.types.InlineKeyboardButton(
                "📝 Создать анкету",
                callback_data="team_action:create",
            )
        )
    return keyboard


def build_teammate_mode_keyboard(
    session_id: str,
) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
    for mode_key, mode_label in TEAMMATE_MODES.items():
        keyboard.add(
            telebot.types.InlineKeyboardButton(
                mode_label,
                callback_data=f"team_mode:{mode_key}:{session_id}",
            )
        )
    return keyboard


def build_party_size_keyboard(
    session_id: str,
) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    for party_key in ("1", "2", "3", "4plus"):
        keyboard.add(
            telebot.types.InlineKeyboardButton(
                PARTY_SIZE_CHOICES[party_key],
                callback_data=f"team_party:{party_key}:{session_id}",
            )
        )
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            PARTY_SIZE_CHOICES["other"],
            callback_data=f"team_party:other:{session_id}",
        )
    )
    return keyboard


def build_privilege_keyboard(
    session_id: str,
) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=3)
    buttons = [
        telebot.types.InlineKeyboardButton(
            "Другая" if privilege_key == "other" else label,
            callback_data=f"team_privilege:{privilege_key}:{session_id}",
        )
        for privilege_key, label in TEAMMATE_PRIVILEGES.items()
    ]
    offset = 0
    for row_size in (3, 3, 2, 2, 3):
        keyboard.row(*buttons[offset : offset + row_size])
        offset += row_size
    return keyboard


def teammate_description_prompt() -> str:
    return (
        "Шаг 4 из 4. Коротко расскажи о себе: требования, стиль игры и есть ли "
        f"микрофон (до {MAX_TEAMMATE_DESCRIPTION_LENGTH} символов)."
    )


def build_teammate_card_keyboard(
    session_id: str,
    profile_id: int,
) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        telebot.types.InlineKeyboardButton(
            "◀️ Назад",
            callback_data=f"team_nav:prev:{session_id}",
        ),
        telebot.types.InlineKeyboardButton(
            "▶️ Вперед",
            callback_data=f"team_nav:next:{session_id}",
        ),
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "✉️ Связаться",
            callback_data=f"team_contact:{profile_id}:{session_id}",
        )
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "↩️ Меню поиска",
            callback_data="team_menu",
        )
    )
    return keyboard


def build_main_menu_keyboard() -> telebot.types.ReplyKeyboardMarkup:
    keyboard = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    keyboard.row(telebot.types.KeyboardButton(PROFILE_BUTTON_TEXT))
    keyboard.row(telebot.types.KeyboardButton(FIND_BUTTON_TEXT))
    keyboard.row(telebot.types.KeyboardButton(REGISTER_BUTTON_TEXT))
    return keyboard


def initialize_database() -> None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS funtime_nicknames (
                telegram_user_id INTEGER PRIMARY KEY,
                nickname TEXT NOT NULL,
                contact TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(funtime_nicknames)"
            ).fetchall()
        }
        if "contact" not in columns:
            connection.execute(
                "ALTER TABLE funtime_nicknames ADD COLUMN contact TEXT"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS teammate_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                server TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'Другое',
                server_number TEXT NOT NULL DEFAULT '',
                privilege TEXT NOT NULL DEFAULT 'Неопределенная',
                looking_for TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        teammate_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(teammate_profiles)"
            ).fetchall()
        }
        if "mode" not in teammate_columns:
            connection.execute(
                "ALTER TABLE teammate_profiles "
                "ADD COLUMN mode TEXT NOT NULL DEFAULT 'Другое'"
            )
        if "server_number" not in teammate_columns:
            connection.execute(
                "ALTER TABLE teammate_profiles "
                "ADD COLUMN server_number TEXT NOT NULL DEFAULT ''"
            )
        if "privilege" not in teammate_columns:
            connection.execute(
                "ALTER TABLE teammate_profiles "
                "ADD COLUMN privilege TEXT NOT NULL DEFAULT 'Неопределенная'"
            )
        connection.execute(
            """
            UPDATE teammate_profiles
            SET server_number = server
            WHERE server_number = ''
            """
        )


def save_registration(
    telegram_user_id: int,
    nickname: str,
    contact: str,
) -> None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.execute(
            """
            INSERT INTO funtime_nicknames (telegram_user_id, nickname, contact)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                nickname = excluded.nickname,
                contact = excluded.contact,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_user_id, nickname, contact),
        )


def get_registration(
    telegram_user_id: int,
) -> tuple[str, str | None] | None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        return connection.execute(
            """
            SELECT nickname, contact
            FROM funtime_nicknames
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        ).fetchone()


def has_teammate_profile(telegram_user_id: int) -> bool:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        return connection.execute(
            "SELECT 1 FROM teammate_profiles WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone() is not None


def save_teammate_profile(
    telegram_user_id: int,
    mode: str,
    privilege: str,
    looking_for: str,
    description: str,
) -> None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.execute(
            """
            INSERT INTO teammate_profiles (
                telegram_user_id, server, mode, server_number, privilege,
                looking_for, description
            )
            VALUES (?, '', ?, '', ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                server = '',
                mode = excluded.mode,
                server_number = '',
                privilege = excluded.privilege,
                looking_for = excluded.looking_for,
                description = excluded.description,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                telegram_user_id,
                mode,
                privilege,
                looking_for,
                description,
            ),
        )


def delete_teammate_profile(telegram_user_id: int) -> bool:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        cursor = connection.execute(
            "DELETE FROM teammate_profiles WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        return cursor.rowcount > 0


def get_teammate_profile_ids(excluded_user_id: int) -> list[int]:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT p.id
            FROM teammate_profiles AS p
            JOIN funtime_nicknames AS n
              ON n.telegram_user_id = p.telegram_user_id
            WHERE p.telegram_user_id != ?
            ORDER BY p.updated_at DESC, p.id DESC
            """,
            (excluded_user_id,),
        ).fetchall()
        return [row[0] for row in rows]


def get_teammate_profile(
    profile_id: int,
) -> tuple[int, str, str, str, str, str] | None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        return connection.execute(
            """
            SELECT n.telegram_user_id, n.nickname, p.mode,
                   p.privilege, p.looking_for, p.description
            FROM teammate_profiles AS p
            JOIN funtime_nicknames AS n
              ON n.telegram_user_id = p.telegram_user_id
            WHERE p.id = ?
            """,
            (profile_id,),
        ).fetchone()


def get_teammate_contact(profile_id: int) -> tuple[str, str | None] | None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        return connection.execute(
            """
            SELECT n.nickname, n.contact
            FROM teammate_profiles AS p
            JOIN funtime_nicknames AS n
              ON n.telegram_user_id = p.telegram_user_id
            WHERE p.id = ?
            """,
            (profile_id,),
        ).fetchone()


def build_bot(token: str) -> telebot.TeleBot:
    bot = telebot.TeleBot(token)
    pending_registrations: dict[tuple[int, int], PendingRegistration] = {}
    pending_teammate_profiles: dict[
        tuple[int, int], PendingTeammateProfile
    ] = {}
    teammate_views: dict[tuple[int, int], TeammateViewSession] = {}

    def clear_pending_flows(request_key: tuple[int, int]) -> None:
        pending_registrations.pop(request_key, None)
        pending_teammate_profiles.pop(request_key, None)
        teammate_views.pop(request_key, None)

    def teammate_card_text(
        view: TeammateViewSession,
    ) -> tuple[str, int] | None:
        if not view.profile_ids:
            return None
        view.index %= len(view.profile_ids)
        profile_id = view.profile_ids[view.index]
        profile = get_teammate_profile(profile_id)
        if profile is None:
            return None

        (
            _owner_id,
            nickname,
            mode,
            privilege,
            looking_for,
            description,
        ) = profile
        text = (
            f"👤 Ник на Funtime: {nickname}\n"
            f"🎮 Режим: {mode}\n"
            f"👑 Привилегия: {privilege}\n"
            f"👥 Размер пати: {looking_for}\n\n"
            f"📝 О себе и требования:\n{description}\n\n"
            f"Анкета {view.index + 1} из {len(view.profile_ids)}"
        )
        return text, profile_id

    def edit_teammate_card(
        chat_id: int,
        message_id: int,
        view: TeammateViewSession,
    ) -> None:
        card = teammate_card_text(view)
        if card is None:
            bot.edit_message_text(
                "Анкета больше недоступна. Обновите список через /find.",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=telebot.types.InlineKeyboardMarkup(),
            )
            return

        text, profile_id = card
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=build_teammate_card_keyboard(
                view.session_id,
                profile_id,
            ),
        )

    def show_teammate_menu(message: telebot.types.Message) -> None:
        if not message.from_user:
            return
        if message.chat.type != "private":
            bot.reply_to(
                message,
                "Для поиска тимейтов откройте личный чат с ботом и отправьте /find.",
            )
            return
        request_key = (message.chat.id, message.from_user.id)
        clear_pending_flows(request_key)
        bot.reply_to(
            message,
            FIND_MENU_TEXT,
            reply_markup=build_teammate_menu_keyboard(
                has_teammate_profile(message.from_user.id)
            ),
        )

    @bot.message_handler(commands=["menu"])
    def send_main_menu(message: telebot.types.Message) -> None:
        if message.from_user:
            clear_pending_flows((message.chat.id, message.from_user.id))
        bot.reply_to(
            message,
            MAIN_MENU_TEXT,
            reply_markup=build_main_menu_keyboard(),
        )

    @bot.message_handler(commands=["profile"])
    def show_profile(message: telebot.types.Message) -> None:
        if not message.from_user:
            return

        clear_pending_flows((message.chat.id, message.from_user.id))
        registration = get_registration(message.from_user.id)
        if registration is None:
            profile_text = "Профиль не найден. Чтобы зарегистрироваться, отправьте /reg."
        else:
            nickname, contact = registration
            profile_text = (
                "Ваш профиль:\n"
                f"Ник на Funtime: {nickname}\n"
                f"Контакт: {contact or 'не указан'}"
            )

        bot.reply_to(
            message,
            profile_text,
            reply_markup=build_main_menu_keyboard(),
        )

    @bot.message_handler(commands=["find"])
    def find_teammate(message: telebot.types.Message) -> None:
        show_teammate_menu(message)

    @bot.message_handler(commands=["start"])
    def send_welcome(message: telebot.types.Message) -> None:
        bot.reply_to(
            message,
            "Привет! Отправьте /reg, чтобы зарегистрироваться на Funtime.",
        )

    @bot.message_handler(commands=["help"])
    def send_help(message: telebot.types.Message) -> None:
        bot.reply_to(
            message,
            "Для регистрации отправьте /reg. Укажите ник на Funtime, "
            "выберите Telegram или Discord кнопкой и введите свой юзернейм. "
            "Юзернейм сохранится в нижнем регистре; для Telegram символ @ в начале необязателен. "
            "Для поиска тимейтов отправьте /find.",
        )

    @bot.message_handler(commands=["reg"])
    def start_registration(message: telebot.types.Message) -> None:
        if not message.from_user:
            return

        request_key = (message.chat.id, message.from_user.id)
        clear_pending_flows(request_key)
        pending_registrations[request_key] = PendingRegistration(
            session_id=secrets.token_hex(4)
        )
        bot.reply_to(
            message,
            "Шаг 1 из 3. Введите ник на Funtime (3–16 символов: "
            "английские буквы, цифры и подчёркивание _).",
        )

    @bot.callback_query_handler(
        func=lambda call: bool(call.data and call.data.startswith("team_"))
    )
    def handle_teammate_callback(call: telebot.types.CallbackQuery) -> None:
        if not call.message:
            bot.answer_callback_query(
                call.id,
                "Это меню больше недоступно. Отправьте /find.",
                show_alert=True,
            )
            return

        if call.message.chat.type != "private":
            bot.answer_callback_query(
                call.id,
                "Поиск тимейтов доступен только в личном чате с ботом.",
                show_alert=True,
            )
            return

        chat_id = call.message.chat.id
        request_key = (chat_id, call.from_user.id)
        parts = (call.data or "").split(":")

        if call.data == "team_menu":
            clear_pending_flows(request_key)
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                FIND_MENU_TEXT,
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=build_teammate_menu_keyboard(
                    has_teammate_profile(call.from_user.id)
                ),
            )
            return

        if len(parts) == 3 and parts[0] == "team_mode":
            profile_draft = pending_teammate_profiles.get(request_key)
            if (
                profile_draft is None
                or profile_draft.session_id != parts[2]
                or profile_draft.stage != "mode"
                or parts[1] not in TEAMMATE_MODES
            ):
                bot.answer_callback_query(
                    call.id,
                    "Выбор режима устарел. Начните анкету заново через /find.",
                    show_alert=True,
                )
                return

            profile_draft.mode = TEAMMATE_MODES[parts[1]]
            profile_draft.stage = "party_size"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                f"Шаг 1 из 4. Выбран режим: {profile_draft.mode}.",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=telebot.types.InlineKeyboardMarkup(),
            )
            bot.send_message(
                chat_id,
                "Шаг 2 из 4. Выбери размер пати кнопкой или введи число от 1 до 10.",
                reply_markup=build_party_size_keyboard(profile_draft.session_id),
            )
            return

        if len(parts) == 3 and parts[0] == "team_party":
            profile_draft = pending_teammate_profiles.get(request_key)
            if (
                profile_draft is None
                or profile_draft.session_id != parts[2]
                or profile_draft.stage != "party_size"
                or parts[1] not in PARTY_SIZE_CHOICES
            ):
                bot.answer_callback_query(
                    call.id,
                    "Выбор размера пати устарел. Начните анкету заново через /find.",
                    show_alert=True,
                )
                return

            profile_draft.party_size = parts[1]
            profile_draft.looking_for = PARTY_SIZE_CHOICES[parts[1]]
            profile_draft.stage = "privilege"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                f"Шаг 2 из 4. Выбран размер пати: "
                f"{profile_draft.looking_for}.",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=telebot.types.InlineKeyboardMarkup(),
            )
            bot.send_message(
                chat_id,
                "Шаг 3 из 4. Выбери привилегию:",
                reply_markup=build_privilege_keyboard(
                    profile_draft.session_id
                ),
            )
            return

        if len(parts) == 3 and parts[0] == "team_privilege":
            profile_draft = pending_teammate_profiles.get(request_key)
            if (
                profile_draft is None
                or profile_draft.session_id != parts[2]
                or profile_draft.stage != "privilege"
                or parts[1] not in TEAMMATE_PRIVILEGES
            ):
                bot.answer_callback_query(
                    call.id,
                    "Выбор привилегии устарел. Начните анкету заново через /find.",
                    show_alert=True,
                )
                return

            profile_draft.privilege = TEAMMATE_PRIVILEGES[parts[1]]
            profile_draft.stage = "description"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                f"Шаг 3 из 4. Выбрана привилегия: {profile_draft.privilege}.",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=telebot.types.InlineKeyboardMarkup(),
            )
            bot.send_message(chat_id, teammate_description_prompt())
            return

        if len(parts) == 2 and parts[0] == "team_action":
            action = parts[1]
            if action in {"create", "edit"}:
                has_profile = has_teammate_profile(call.from_user.id)
                if action == "create" and has_profile:
                    bot.answer_callback_query(
                        call.id,
                        "Анкета уже есть. Выберите «Редактировать анкету».",
                        show_alert=True,
                    )
                    return
                if action == "edit" and not has_profile:
                    bot.answer_callback_query(
                        call.id,
                        "Сначала создайте анкету.",
                        show_alert=True,
                    )
                    return

                registration = get_registration(call.from_user.id)
                if registration is None or not registration[1]:
                    bot.answer_callback_query(
                        call.id,
                        "Сначала завершите регистрацию командой /reg.",
                        show_alert=True,
                    )
                    return

                clear_pending_flows(request_key)
                pending_teammate_profiles[request_key] = PendingTeammateProfile(
                    session_id=secrets.token_hex(4),
                    editing=action == "edit",
                )
                bot.answer_callback_query(call.id)
                bot.edit_message_text(
                    (
                        "Редактирование анкеты началось. "
                        "Пройдите все шаги заново; текущая анкета останется "
                        "без изменений, пока вы не сохраните новую."
                        if action == "edit"
                        else "Создание анкеты началось. Ответьте на вопросы в чате."
                    ),
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                    reply_markup=telebot.types.InlineKeyboardMarkup(),
                )
                bot.send_message(
                    chat_id,
                    "Шаг 1 из 4. Выбери режим:",
                    reply_markup=build_teammate_mode_keyboard(
                        pending_teammate_profiles[request_key].session_id
                    ),
                )
                return

            if action == "view":
                if not has_teammate_profile(call.from_user.id):
                    bot.answer_callback_query(
                        call.id,
                        "Сначала создайте свою анкету.",
                        show_alert=True,
                    )
                    return
                clear_pending_flows(request_key)
                profile_ids = get_teammate_profile_ids(call.from_user.id)
                if not profile_ids:
                    bot.answer_callback_query(call.id)
                    bot.edit_message_text(
                        "Пока нет анкет других игроков.",
                        chat_id=chat_id,
                        message_id=call.message.message_id,
                        reply_markup=build_teammate_menu_keyboard(
                            has_teammate_profile(call.from_user.id)
                        ),
                    )
                    return

                view = TeammateViewSession(
                    session_id=secrets.token_hex(4),
                    profile_ids=profile_ids,
                )
                teammate_views[request_key] = view
                bot.answer_callback_query(call.id)
                edit_teammate_card(
                    chat_id,
                    call.message.message_id,
                    view,
                )
                return

            if action == "delete":
                deleted = delete_teammate_profile(call.from_user.id)
                teammate_views.pop(request_key, None)
                pending_teammate_profiles.pop(request_key, None)
                bot.answer_callback_query(call.id)
                bot.edit_message_text(
                    "✅ Ваша анкета удалена."
                    if deleted
                    else "У вас уже нет сохранённой анкеты.",
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                    reply_markup=build_teammate_menu_keyboard(has_profile=False),
                )
                return

        if (
            len(parts) == 3
            and parts[0] == "team_nav"
            and parts[1] in {"prev", "next"}
        ):
            view = teammate_views.get(request_key)
            if view is None or view.session_id != parts[2]:
                bot.answer_callback_query(
                    call.id,
                    "Просмотр анкет устарел. Отправьте /find.",
                    show_alert=True,
                )
                return

            profile_ids = get_teammate_profile_ids(call.from_user.id)
            if not profile_ids:
                teammate_views.pop(request_key, None)
                bot.answer_callback_query(call.id)
                bot.edit_message_text(
                    "Пока нет анкет других игроков.",
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                    reply_markup=build_teammate_menu_keyboard(
                        has_teammate_profile(call.from_user.id)
                    ),
                )
                return

            old_profile_id = (
                view.profile_ids[view.index]
                if view.profile_ids and view.index < len(view.profile_ids)
                else None
            )
            if old_profile_id in profile_ids:
                current_index = profile_ids.index(old_profile_id)
            else:
                current_index = min(view.index, len(profile_ids) - 1)

            if parts[1] == "prev":
                view.index = (current_index - 1) % len(profile_ids)
            else:
                view.index = (current_index + 1) % len(profile_ids)
            view.profile_ids = profile_ids

            bot.answer_callback_query(call.id)
            edit_teammate_card(chat_id, call.message.message_id, view)
            return

        if len(parts) == 3 and parts[0] == "team_contact":
            view = teammate_views.get(request_key)
            try:
                profile_id = int(parts[1])
            except ValueError:
                profile_id = -1

            if (
                view is None
                or view.session_id != parts[2]
                or profile_id not in view.profile_ids
            ):
                bot.answer_callback_query(
                    call.id,
                    "Эта анкета больше недоступна.",
                    show_alert=True,
                )
                return

            profile = get_teammate_profile(profile_id)
            contact = get_teammate_contact(profile_id)
            if (
                profile is None
                or contact is None
                or profile[0] == call.from_user.id
            ):
                bot.answer_callback_query(
                    call.id,
                    "Не удалось открыть контакт этой анкеты.",
                    show_alert=True,
                )
                return

            nickname, contact_text = contact
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                f"✉️ Контакт игрока {nickname}:\n"
                f"{contact_text or 'Контакт не указан.'}",
            )
            return

        bot.answer_callback_query(
            call.id,
            "Неизвестное действие. Отправьте /find.",
            show_alert=True,
        )

    @bot.callback_query_handler(
        func=lambda call: bool(call.data and call.data.startswith("reg_contact:"))
    )
    def choose_contact_platform(call: telebot.types.CallbackQuery) -> None:
        parts = (call.data or "").split(":")
        if (
            len(parts) != 3
            or parts[1] not in CONTACT_PLATFORMS
            or not call.message
        ):
            bot.answer_callback_query(
                call.id,
                "Не удалось определить контакт. Начните заново командой /reg.",
                show_alert=True,
            )
            return

        request_key = (call.message.chat.id, call.from_user.id)
        registration = pending_registrations.get(request_key)
        if (
            registration is None
            or registration.session_id != parts[2]
            or registration.nickname is None
        ):
            bot.answer_callback_query(
                call.id,
                "Эта регистрация устарела. Начните заново командой /reg.",
                show_alert=True,
            )
            return

        if registration.contact_platform is not None:
            bot.answer_callback_query(call.id, "Контакт уже выбран.")
            return

        registration.contact_platform = parts[1]
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"Шаг 3 из 3. {contact_username_prompt(parts[1])}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=telebot.types.InlineKeyboardMarkup(),
        )

    @bot.message_handler(content_types=["text"])
    def handle_text(message: telebot.types.Message) -> None:
        if not message.text:
            return

        if message.text == PROFILE_BUTTON_TEXT:
            show_profile(message)
            return
        if message.text == FIND_BUTTON_TEXT:
            find_teammate(message)
            return
        if message.text == REGISTER_BUTTON_TEXT:
            start_registration(message)
            return

        if message.from_user:
            request_key = (message.chat.id, message.from_user.id)
            teammate_profile = pending_teammate_profiles.get(request_key)
            if teammate_profile is not None:
                value = message.text.strip()
                if value.startswith("/"):
                    if teammate_profile.stage == "privilege":
                        bot.reply_to(
                            message,
                            "Шаг 3 из 4. Выбери привилегию:",
                            reply_markup=build_privilege_keyboard(
                                teammate_profile.session_id
                            ),
                        )
                        return
                    prompts = {
                        "mode": "Выберите режим кнопкой под сообщением.",
                        "party_size": (
                            "Выберите размер пати кнопкой или введите число от 1 до 10."
                        ),
                        "privilege": "Выберите привилегию кнопкой.",
                        "description": "Коротко опишите требования, стиль игры и наличие микрофона.",
                    }
                    prompt = prompts[teammate_profile.stage]
                    bot.reply_to(message, prompt)
                    return

                if teammate_profile.stage == "mode":
                    bot.reply_to(
                        message,
                        "Выберите режим кнопкой под сообщением.",
                        reply_markup=build_teammate_mode_keyboard(
                            teammate_profile.session_id
                        ),
                    )
                    return

                if teammate_profile.stage == "party_size":
                    if re.fullmatch(r"(?:[1-9]|10)", value) is None:
                        bot.reply_to(
                            message,
                            "⚠️ Выберите размер пати кнопкой или введите число от 1 до 10.",
                            reply_markup=build_party_size_keyboard(
                                teammate_profile.session_id
                            ),
                        )
                        return

                    party_size = int(value)
                    teammate_profile.party_size = str(party_size)
                    teammate_profile.looking_for = f"+{party_size}"
                    teammate_profile.stage = "privilege"
                    bot.reply_to(
                        message,
                        "Шаг 3 из 4. Выбери привилегию:",
                        reply_markup=build_privilege_keyboard(
                            teammate_profile.session_id
                        ),
                    )
                    return

                if teammate_profile.stage == "privilege":
                    bot.reply_to(
                        message,
                        "Выберите привилегию кнопкой под сообщением.",
                        reply_markup=build_privilege_keyboard(
                            teammate_profile.session_id
                        ),
                    )
                    return

                description = value
                if not description:
                    bot.reply_to(
                        message,
                        "Описание не должно быть пустым. Напиши пару слов "
                        "о себе и кого ищешь.",
                    )
                    return
                if len(description) > MAX_TEAMMATE_DESCRIPTION_LENGTH:
                    bot.reply_to(
                        message,
                        f"Описание слишком длинное. Максимум — "
                        f"{MAX_TEAMMATE_DESCRIPTION_LENGTH} символов. "
                        "Сократи его и отправь ещё раз.",
                    )
                    return

                save_teammate_profile(
                    message.from_user.id,
                    teammate_profile.mode or TEAMMATE_MODES["other"],
                    teammate_profile.privilege
                    or TEAMMATE_PRIVILEGES["other"],
                    teammate_profile.looking_for or "",
                    description,
                )
                pending_teammate_profiles.pop(request_key, None)
                teammate_views.pop(request_key, None)
                bot.reply_to(
                    message,
                    (
                        "✅ Анкета обновлена!"
                        if teammate_profile.editing
                        else "✅ Анкета сохранена! Другие игроки увидят её в поиске."
                    ),
                    reply_markup=build_teammate_menu_keyboard(has_profile=True),
                )
                return

            registration = pending_registrations.get(request_key)
            if registration is not None:
                value = message.text.strip()

                if value.startswith("/"):
                    if registration.nickname is None:
                        prompt = "Сначала введите ник на Funtime обычным сообщением."
                    elif registration.contact_platform is None:
                        prompt = "Сначала выберите Telegram или Discord кнопкой."
                    else:
                        prompt = contact_username_prompt(
                            registration.contact_platform
                        )
                    bot.reply_to(message, prompt)
                    return

                if registration.nickname is None:
                    nickname = message.text
                    validation_error = validate_funtime_nickname(nickname)
                    if validation_error:
                        bot.reply_to(
                            message,
                            f"{validation_error}\n"
                            "Введите ник на Funtime ещё раз по этим правилам.",
                        )
                        return

                    registration.nickname = nickname
                    bot.reply_to(
                        message,
                        "Шаг 2 из 3. Выберите контакт для связи:",
                        reply_markup=build_contact_keyboard(registration.session_id),
                    )
                    return

                if registration.contact_platform is None:
                    bot.reply_to(
                        message,
                        "Сначала выберите Telegram или Discord кнопкой под сообщением.",
                    )
                    return

                platform_key = registration.contact_platform
                username = normalize_contact_username(platform_key, value)
                if not username:
                    bot.reply_to(message, contact_username_prompt(platform_key))
                    return

                if platform_key == "discord":
                    validation_error = validate_discord_username(username)
                else:
                    validation_error = validate_telegram_username(username)

                if validation_error:
                    bot.reply_to(
                        message,
                        f"{validation_error}\n{contact_username_prompt(platform_key)}",
                    )
                    return

                platform = CONTACT_PLATFORMS[platform_key]
                contact = f"{platform}: {username}"
                save_registration(
                    message.from_user.id,
                    registration.nickname,
                    contact,
                )
                del pending_registrations[request_key]
                send_main_menu(message)
                return

        bot.reply_to(message, message.text)

    return bot


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error(
            "Missing TELEGRAM_BOT_TOKEN. Add your bot token in Replit Secrets."
        )
        return 1

    initialize_database()

    # Keep library errors out of logs because API request URLs can contain the
    # bot token. Report only sanitized connection status below.
    telebot.logger.setLevel(logging.CRITICAL)

    bot = build_bot(token)
    try:
        bot_info = bot.get_me()
    except Exception as error:
        api_error_code = getattr(error, "error_code", None)
        if isinstance(api_error_code, int):
            logger.error(
                "Telegram returned API error %d. Check the bot token in Replit Secrets.",
                api_error_code,
            )
        else:
            logger.error(
                "Telegram connection failed (%s). Check the token and network connection.",
                type(error).__name__,
            )
        return 1

    logger.info("Connected to Telegram. Starting long polling.")
    if bot_info.username:
        logger.info("Bot username: @%s", bot_info.username)

    try:
        bot.infinity_polling(
            skip_pending=True,
            timeout=30,
            long_polling_timeout=30,
            logger_level=logging.CRITICAL,
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped.")

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())