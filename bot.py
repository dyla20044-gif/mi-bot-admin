import asyncio
import logging
import json
import re
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import requests
from collections import deque
import datetime
import time
import random
from webserver import keep_alive

# 1. Configuración de credenciales y constantes
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TELEGRAM_CHANNEL_ID = -1001945286271
ADMIN_ID = 6115976248
BASE_TMDB_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
MOVIES_DB_FILE = "movies.json"

# Constantes para Trakt.tv
TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID")
TRAKT_CLIENT_SECRET = os.getenv("TRAKT_CLIENT_SECRET")
TRAKT_BASE_URL = "https://api.trakt.tv"

# Almacenamiento de posts programados y posts recientes
scheduled_posts = asyncio.Queue()
recent_posts = deque(maxlen=20)

# Almacenamiento temporal para solicitudes de usuarios y datos de admins
user_requests = {}
admin_data = {}
memes = [
    {"photo_url": "https://i.imgflip.com/64s72q.jpg", "caption": "Cuando te dicen que hay una película nueva... y es la que no querías."},
    {"photo_url": "https://i.imgflip.com/71j22e.jpg", "caption": "Yo esperando la película que pedí en el canal..."},
    {"photo_url": "https://i.imgflip.com/83p14j.jpg", "caption": "Mi reacción cuando el bot me dice que la película ya está en el catálogo."},
    {"photo_url": "https://i.imgflip.com/4q3e3i.jpg", "caption": "Cuando me entero que la película que quiero ya está disponible en alta calidad."},
    {"photo_url": "https://i.imgflip.com/776k1w.jpg", "caption": "Yo después de ver 3 películas seguidas en un día."}
]

# Configuración del logging
logging.basicConfig(level=logging.INFO)

# 2. Inicialización del bot, dispatcher y la "base de datos"
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
movies_db = {}
AUTO_POST_COUNT = 4
MOVIES_PER_PAGE = 5

# Estados para la máquina de estados de aiogram
class MovieUploadStates(StatesGroup):
    waiting_for_movie_info = State()
    waiting_for_requested_movie_link = State()

class MovieRequestStates(StatesGroup):
    waiting_for_movie_name = State()

class AdminStates(StatesGroup):
    waiting_for_auto_post_count = State()

# 3. Funciones auxiliares para la base de datos de películas
def load_movies_db():
    global movies_db
    try:
        with open(MOVIES_DB_FILE, "r", encoding="utf-8") as f:
            movies_db = json.load(f)
            logging.info(f"Se cargaron {len(movies_db)} películas de la base de datos.")
    except (FileNotFoundError, json.JSONDecodeError):
        logging.warning("No se encontró el archivo de la base de datos o está vacío. Se creará uno nuevo.")
        movies_db = {}

def save_movies_db():
    with open(MOVIES_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(movies_db, f, ensure_ascii=False, indent=4)
        logging.info("Base de datos de películas guardada con éxito.")

def find_movie_in_db(title_to_find):
    for main_title, movie_data in movies_db.items():
        if "names" in movie_data and title_to_find.lower() in [name.lower() for name in movie_data["names"]]:
            return main_title, movie_data
        elif main_title.lower() == title_to_find.lower():
            return main_title, movie_data
    return None, None

# 4. Funciones auxiliares para la API de TMDB
def get_movie_id_by_title(title, year=None):
    url = f"{BASE_TMDB_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "es-ES"}
    if year:
        params["year"] = year

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        results = response.json().get("results", [])
        if results:
            return results[0].get("id")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al buscar película en TMDB por título: {e}")
        return []

def get_movie_details(movie_id):
    url = f"{BASE_TMDB_URL}/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES"}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conectar con la API de TMDB: {e}")
        return None

def get_popular_movies():
    url = f"{BASE_TMDB_URL}/movie/popular"
    params = {"api_key": TMDB_API_KEY, "language": "es-ES", "page": 1}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json().get("results", [])
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al obtener películas populares de TMDB: {e}")
        return []

def trakt_api_search_movie(title):
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID
    }
    url = f"{TRAKT_BASE_URL}/search/movie"
    params = {"query": title}

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        results = response.json()
        if results:
            for result in results:
                tmdb_id = result.get("movie", {}).get("ids", {}).get("tmdb")
                if tmdb_id:
                    return tmdb_id
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al buscar película en Trakt.tv: {e}")
        return None

# 5. Creación del mensaje de la película
def create_movie_message(movie_data, movie_link=None):
    title = movie_data.get("title", "Título no disponible")
    overview = movie_data.get("overview", "Sinopsis no disponible")
    release_date = movie_data.get("release_date", "Fecha no disponible")
    vote_average = movie_data.get("vote_average", 0)
    poster_path = movie_data.get("poster_path")

    if not overview.strip():
        overview = "Sinopsis no disponible."

    text = (
        f"<b>🎬 {title}</b>\n\n"
        f"<i>Sinopsis:</i> {overview}\n\n"
        f"📅 <b>Fecha de estreno:</b> {release_date}\n"
        f"⭐ <b>Puntuación:</b> {vote_average:.1f}/10"
    )

    if movie_link:
        text += f'\n\n<a href="{movie_link}">Ver la película aquí</a>'

    poster_url = f"{POSTER_BASE_URL}{poster_path}" if poster_path else None

    return text, poster_url

# 6. Funciones de gestión de mensajes en el canal
async def delete_old_post(movie_id_tmdb):
    found_key = None
    for key, data in movies_db.items():
        if data.get("id") == movie_id_tmdb:
            found_key = key
            break

    if found_key:
        old_message_id = movies_db[found_key].get("last_message_id")
        if old_message_id:
            try:
                await bot.delete_message(chat_id=TELEGRAM_CHANNEL_ID, message_id=old_message_id)
                logging.info(f"Mensaje anterior con ID {old_message_id} de '{found_key}' eliminado.")
                movies_db[found_key]["last_message_id"] = None
                save_movies_db()
            except Exception as e:
                logging.error(f"Error al intentar borrar el mensaje {old_message_id}: {e}")

async def send_movie_post(chat_id, movie_data, movie_link):
    text, poster_url = create_movie_message(movie_data, movie_link)

    post_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎬 ¿Quieres pedir una película? Pídela aquí 👇", url="https://t.me/dylan_ad_bot")]
    ])

    try:
        if poster_url:
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=poster_url,
                caption=text,
                reply_markup=post_keyboard
            )
        else:
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=post_keyboard
            )

        if chat_id == TELEGRAM_CHANNEL_ID:
            movie_key = next((k for k, v in movies_db.items() if movie_data.get("id") == v.get("id")), None)
            if movie_key:
                movies_db[movie_key]["last_message_id"] = message.message_id
                save_movies_db()

        return True, message.message_id
    except Exception as e:
        logging.error(f"Error al enviar la publicación: {e}")
        return False, None

# 7. Manejadores de comandos y botones
@dp.message(Command("start"))
async def start_command(message: types.Message):
    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        keyboard = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="➕ Agregar película"), types.KeyboardButton(text="📋 Ver catálogo")],
                [types.KeyboardButton(text="⚙️ Configuración auto-publicación")]
            ],
            resize_keyboard=True
        )
        await message.reply(
            "¡Hola, Administrador! Elige una opción:\n\n"
            "**Opciones de Administrador:**\n"
            "➕ **Agregar película:** Agrega una nueva película a la base de datos.\n"
            "📋 **Ver catálogo:** Revisa las películas existentes y publícalas si lo deseas.\n"
            "⚙️ **Configuración auto-publicación:** Cambia la cantidad de publicaciones automáticas al día.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📽️ Pedir una película", callback_data="ask_for_movie")],
            [types.InlineKeyboardButton(text="🎞️ Estrenos", callback_data="show_estrenos")]
        ])
        await message.reply(
            "¡Hola! Soy un bot que te ayuda a encontrar tus películas favoritas.\n\n"
            "**¿Qué puedo hacer?**\n"
            "🎬 **Buscar películas:** Haz clic en el botón de abajo para solicitar una película. Si está en mi base de datos, la publicaré al instante en el canal.\n"
            "🎞️ **Estrenos:** Descubre qué películas populares ya están en nuestro catálogo.\n"
            "🔗 **Acceso rápido:** Si la película que buscas ya está en el canal, te enviaré un enlace para que la encuentres fácilmente.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

# Manejador para eliminar mensajes de spam (usa el dominio como filtro)
@dp.message(F.text.contains("ordershunter.ru"))
async def delete_spam_message(message: types.Message):
    try:
        await message.delete()
    except Exception as e:
        logging.error(f"No se pudo eliminar el mensaje de spam: {e}")

@dp.message(F.text == "➕ Agregar película")
async def add_movie_start_by_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return

    await message.reply(
        "Por favor, envía el título principal y todos los nombres de la película, seguidos por el enlace, en este formato:\n"
        "Título Principal (Año) | Nombre_1, Nombre_2, Nombre_3 | Enlace_de_la_película"
    )
    await state.set_state(MovieUploadStates.waiting_for_movie_info)

# <--- NUEVA FUNCIÓN: Ver catálogo de películas
@dp.message(F.text == "📋 Ver catálogo")
async def view_catalog_by_text(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return

    if not movies_db:
        await message.reply("Aún no hay películas en la base de datos.")
        return

    await send_catalog_page(message.chat.id, 0)

async def send_catalog_page(chat_id, page):
    movie_items = list(movies_db.items())
    start = page * MOVIES_PER_PAGE
    end = start + MOVIES_PER_PAGE

    page_movies = movie_items[start:end]
    total_pages = (len(movie_items) + MOVIES_PER_PAGE - 1) // MOVIES_PER_PAGE

    text = f"**Catálogo de Películas** (Página {page + 1}/{total_pages})\n\n"
    keyboard_buttons = []

    for _, data in page_movies:
        title = data.get("names")[0] if "names" in data and data.get("names") else "Título desconocido"
        movie_id = data.get("id")
        keyboard_buttons.append([types.InlineKeyboardButton(text=f"Publicar '{title}'", callback_data=f"publish_from_catalog_{movie_id}")])

    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(types.InlineKeyboardButton(text="⬅️ Anterior", callback_data=f"catalog_page_{page-1}"))
    if page + 1 < total_pages:
        pagination_buttons.append(types.InlineKeyboardButton(text="Siguiente ➡️", callback_data=f"catalog_page_{page+1}"))

    if pagination_buttons:
        keyboard_buttons.append(pagination_buttons)

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

# <--- NUEVA FUNCIÓN: Manejador de navegación del catálogo
@dp.callback_query(F.data.startswith("catalog_page_"))
async def navigate_catalog(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split("_")[-1])
    await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    await send_catalog_page(callback_query.message.chat.id, page)

# <--- NUEVA FUNCIÓN: Publicar película desde el catálogo
@dp.callback_query(F.data.startswith("publish_from_catalog_"))
async def publish_from_catalog(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split("_")[-1])

    movie_info = next((v for v in movies_db.values() if v['id'] == movie_id), None)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return

    movie_data = get_movie_details(movie_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede publicar.", show_alert=True)
        return

    await delete_old_post(movie_id)

    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"))

    if success:
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada con éxito.", show_alert=True)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)

@dp.message(F.text == "📋 Ver películas")
async def view_movies_by_text(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return

    if not movies_db:
        await message.reply("Aún no hay películas en la base de datos.")
        return

    movie_lines = []
    for main_title, data in movies_db.items():
        names = data.get("names", [])
        if names:
            main_name = names[0]
            other_names = names[1:]
            if other_names:
                movie_lines.append(f"- {main_name} ({', '.join(other_names)})")
            else:
                movie_lines.append(f"- {main_name}")
        else:
            movie_lines.append(f"- {main_title} (Sin nombres alternativos)")

    movie_list = "\n".join(movie_lines)
    await message.reply(f"Películas en la base de datos:\n\n{movie_list}")

@dp.message(F.text == "⚙️ Configuración auto-publicación")
async def auto_post_config(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply("No tienes permiso para esta acción.")
        return

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="2 películas al día", callback_data="set_auto_2")],
        [types.InlineKeyboardButton(text="4 películas al día", callback_data="set_auto_4")],
        [types.InlineKeyboardButton(text="6 películas al día", callback_data="set_auto_6")],
        [types.InlineKeyboardButton(text="8 películas al día", callback_data="set_auto_8")]
    ])
    await message.reply("Elige cuántas películas quieres que se publiquen automáticamente cada día:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("set_auto_"))
async def set_auto_post_count(callback_query: types.CallbackQuery):
    global AUTO_POST_COUNT
    AUTO_POST_COUNT = int(callback_query.data.split("_")[2])

    await bot.answer_callback_query(callback_query.id, f"Publicación automática configurada para {AUTO_POST_COUNT} películas al día.")
    await bot.edit_message_text(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.message_id,
        text=f"✅ Publicación automática configurada para {AUTO_POST_COUNT} películas al día."
    )

@dp.message(MovieUploadStates.waiting_for_movie_info)
async def add_movie_info(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply("No tienes permiso para usar esta función.")
        await state.clear()
        return

    await state.clear()
    parts = message.text.split("|")
    if len(parts) < 3:
        await message.reply("Formato incorrecto. Por favor, usa el formato: Título Principal (Año) | Nombres | Enlace")
        return

    main_title_with_year = parts[0].strip()
    names_str = parts[1].strip()
    movie_link = parts[2].strip()

    match = re.search(r'\((19|20)\d{2}\)', main_title_with_year)
    if not match:
        await message.reply("Formato de año incorrecto. Debe ser (YYYY).")
        return

    year = match.group(0).replace('(', '').replace(')', '')
    main_title = main_title_with_year.replace(match.group(0), '').strip()

    names = [name.strip() for name in names_str.split(',')]

    await message.reply(f"Buscando '{main_title}' del año {year} en TMDB...")

    movie_id = get_movie_id_by_title(main_title, year)
    if not movie_id:
        await message.reply(
            f"No se pudo encontrar la película '{main_title}' del año {year} en TMDB. "
            "Por favor, asegúrate de escribir el título y el año correctamente."
        )
        return

    movies_db[main_title.lower()] = {
        "names": names,
        "id": movie_id,
        "link": movie_link,
        "last_message_id": None
    }
    save_movies_db()

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="➕ Agregar otra película", callback_data="add_movie_again")],
        [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_now_{movie_id}")],
        [types.InlineKeyboardButton(text="⏰ Programar publicación", callback_data=f"schedule_{movie_id}")]
    ])
    await message.reply("✅ Tu película fue agregada correctamente. ¿Qué quieres hacer ahora?", reply_markup=keyboard)

@dp.callback_query(F.data == "add_movie_again")
async def add_movie_again_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, envía la información de la siguiente película en el formato: Título Principal (Año) | Nombres | Enlace"
    )
    await state.set_state(MovieUploadStates.waiting_for_movie_info)

@dp.callback_query(F.data.startswith("publish_now_"))
async def publish_now_callback(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split("_")[2])

    movie_data = get_movie_details(movie_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede publicar.", show_alert=True)
        return

    await delete_old_post(movie_id)

    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, next(v['link'] for v in movies_db.values() if v['id'] == movie_id))

    if success:
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada con éxito.", show_alert=True)
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)

@dp.callback_query(F.data.startswith("schedule_"))
async def schedule_callback(callback_query: types.CallbackQuery):
    movie_id = int(callback_query.data.split("_")[1])

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="En 30 minutos", callback_data=f"schedule_30m_{movie_id}")],
        [types.InlineKeyboardButton(text="En 1 hora", callback_data=f"schedule_1h_{movie_id}")]
    ])

    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Elige cuándo quieres programar la publicación:",
        reply_markup=keyboard
    )
    await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)

@dp.callback_query(F.data.startswith("schedule_"))
async def final_schedule_callback(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    delay_type = parts[1]
    movie_id = int(parts[2])

    delay_minutes = 0
    if delay_type == "30m":
        delay_minutes = 30
    elif delay_type == "1h":
        delay_minutes = 60

    movie_info = next((v for v in movies_db.values() if v['id'] == movie_id), None)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return

    await scheduled_posts.put((movie_info, delay_minutes))

    await bot.answer_callback_query(callback_query.id, f"✅ Publicación programada para dentro de {delay_minutes} minutos.", show_alert=True)
    await bot.edit_message_text(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.message_id,
        text=f"✅ Película programada para publicación."
    )

# <--- CORRECCIÓN DE ERROR: Ahora ordena los estrenos correctamente
@dp.callback_query(F.data == "show_estrenos")
async def show_estrenos_callback(callback_query: types.CallbackQuery):
    if not movies_db:
        await bot.answer_callback_query(callback_query.id, "Aún no hay películas en el catálogo. ¡Pronto habrá!", show_alert=True)
        return

    # Ordenar las películas por last_message_id, moviendo los None al final
    sorted_movies = sorted(movies_db.values(), key=lambda x: x.get('last_message_id', float('-inf')), reverse=True)
    recent_movies = sorted_movies[:10]

    text = "**🎞️ ¡Estrenos!**\n\nAquí tienes las últimas películas publicadas en el canal. Si quieres ver una, solo escribe su nombre completo.\n\n"

    if not recent_movies or all(m.get('last_message_id') is None for m in recent_movies):
      text = "**🎞️ ¡Estrenos!**\n\nNo hay estrenos recientes publicados en el canal, pero aquí tienes una lista de películas de nuestra base de datos que podrían interesarte.\n\n"
      recent_movies = random.sample(list(movies_db.values()), min(len(movies_db), 10))


    for movie in recent_movies:
        title = movie.get("names")[0] if "names" in movie and movie.get("names") else "Título desconocido"
        text += f"- {title}\n"

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📽️ Pedir una película", callback_data="ask_for_movie")]
    ])

    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(callback_query.message.chat.id, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "ask_for_movie")
async def ask_for_movie_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        chat_id=callback_query.message.chat.id,
        text="Por favor, escribe el nombre correcto de tu película."
    )
    await state.set_state(MovieRequestStates.waiting_for_movie_name)

@dp.message(MovieRequestStates.waiting_for_movie_name)
async def process_movie_request(message: types.Message, state: FSMContext):
    movie_title = message.text.strip()
    await state.clear()

    main_title, movie_info = find_movie_in_db(movie_title)

    if not movie_info:
        trakt_id = trakt_api_search_movie(movie_title)

        if trakt_id:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_now_from_trakt_{trakt_id}_{message.from_user.id}")]
            ])

            user_requests[movie_title.lower()] = message.from_user.id

            await bot.send_message(
                ADMIN_ID,
                f"El usuario {message.from_user.full_name} (@{message.from_user.username}) ha solicitado la película: <b>{movie_title}</b>\n\n"
                f"ℹ️ **Se encontró en Trakt.tv con ID de TMDB:** `{trakt_id}`",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )

            keyboard_user = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📽️ Pedir otra película", callback_data="ask_for_movie")]
            ])
            await message.reply(
                "La película que solicitaste no está en la base de datos, pero el administrador ha sido notificado para que pueda revisarla. ¡Pronto estará lista!",
                reply_markup=keyboard_user
            )
        else:
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="➕ Agregar película solicitada", callback_data=f"add_requested_{movie_title}_{message.from_user.id}")]
            ])

            user_requests[movie_title.lower()] = message.from_user.id

            await bot.send_message(
                ADMIN_ID,
                f"El usuario {message.from_user.full_name} (@{message.from_user.username}) ha solicitado la película: <b>{movie_title}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )

            keyboard_user = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="📽️ Pedir otra película", callback_data="ask_for_movie")]
            ])
            await message.reply(
                "Lo siento, esa película aún no está disponible. El administrador ha sido notificado de tu solicitud. ¡Pronto estará lista!",
                reply_markup=keyboard_user
            )
        return

    movie_id = movie_info.get("id")
    movie_link = movie_info.get("link")

    if not movie_id or not movie_link:
        await message.reply("Ocurrió un error. El administrador debe volver a subirla. Intenta contactarlo.")
        return

    movie_data = get_movie_details(movie_id)
    if not movie_data:
        await message.reply(
            "Lo siento, hubo un problema al obtener la información de la película. Por favor, intenta de nuevo más tarde."
        )
        return

    await delete_old_post(movie_data.get("id"))

    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_link)

    if success:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📽️ Pedir otra película", callback_data="ask_for_movie")]
        ])
        await message.reply(
            f"✅ Tu película fue publicada en el canal principal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
            reply_markup=keyboard
        )
    else:
        await message.reply("Ocurrió un error al intentar publicar la película. Por favor, contacta al administrador.")

@dp.callback_query(F.data.startswith("publish_now_from_trakt_"))
async def publish_from_trakt(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return

    parts = callback_query.data.split('_')
    tmdb_id = int(parts[3])
    user_id = int(parts[4])

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información completa de la película desde TMDB.", show_alert=True)
        return

    await state.update_data(tmdb_id=tmdb_id, movie_title=movie_data.get("title"), user_request_id=user_id)

    await bot.send_message(
        ADMIN_ID,
        f"Por favor, ahora envía el enlace de la película '{movie_data.get('title')}' para publicarla."
    )

    await bot.answer_callback_query(callback_query.id)
    await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)

    await state.set_state(MovieUploadStates.waiting_for_requested_movie_link)

@dp.callback_query(F.data.startswith("add_requested_"))
async def add_requested_movie_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return

    parts = callback_query.data.split('_')
    requested_title = parts[2]
    user_id = int(parts[3])

    movie_id = get_movie_id_by_title(requested_title)
    if not movie_id:
        await bot.send_message(callback_query.from_user.id, "No se pudo encontrar la película en TMDB. No se puede continuar.")
        return

    await state.update_data(tmdb_id=movie_id, movie_title=requested_title, user_request_id=user_id)

    await bot.send_message(
        callback_query.from_user.id,
        f"Por favor, ahora envía el enlace de la película '{requested_title}'."
    )
    await state.set_state(MovieUploadStates.waiting_for_requested_movie_link)

@dp.message(MovieUploadStates.waiting_for_requested_movie_link)
async def process_requested_movie_link(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply("No tienes permiso para usar esta función.")
        await state.clear()
        return

    movie_link = message.text.strip()
    user_data = await state.get_data()

    tmdb_id = user_data.get("tmdb_id")
    movie_title = user_data.get("movie_title")
    user_request_id = user_data.get("user_request_id")

    if not tmdb_id or not movie_title or not user_request_id:
        await message.reply("Ocurrió un error. Por favor, comienza el proceso de nuevo.")
        await state.clear()
        return

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await message.reply("No se pudo obtener la información de la película desde TMDB. No se puede guardar.")
        await state.clear()
        return

    main_title = movie_data.get("title")
    names = [main_title]
    if movie_data.get("original_title") != main_title:
        names.append(movie_data.get("original_title"))

    movies_db[main_title.lower()] = {
        "names": names,
        "id": tmdb_id,
        "link": movie_link,
        "last_message_id": None
    }
    save_movies_db()

    await state.clear()

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎬 Publicar ahora", callback_data=f"publish_requested_{tmdb_id}_{user_request_id}")],
        [types.InlineKeyboardButton(text="🔔 Avisar al usuario", callback_data=f"notify_user_{user_request_id}_{tmdb_id}")]
    ])

    await message.reply("✅ Película agregada a la base de datos y lista para publicar. ¿Qué quieres hacer ahora?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("publish_requested_"))
async def publish_requested_movie(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return

    parts = callback_query.data.split('_')
    tmdb_id = int(parts[2])
    user_request_id = int(parts[3])

    movie_info = next((v for v in movies_db.values() if v['id'] == tmdb_id), None)
    if not movie_info:
        await bot.answer_callback_query(callback_query.id, "Error: película no encontrada en la base de datos.", show_alert=True)
        return

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede publicar.", show_alert=True)
        return

    await delete_old_post(tmdb_id)

    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"))

    if success:
        await bot.answer_callback_query(callback_query.id, "✅ Película publicada con éxito.", show_alert=True)
        if user_request_id:
            try:
                await bot.send_message(
                    user_request_id,
                    f"✅ La película que solicitaste, '{movie_data.get('title')}', ya está disponible en el canal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logging.error(f"Error al notificar al usuario {user_request_id}: {e}")
        await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=callback_query.message.message_id)
    else:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al publicar la película.", show_alert=True)

@dp.callback_query(F.data.startswith("notify_user_"))
async def notify_user(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return

    parts = callback_query.data.split('_')
    user_request_id = int(parts[2])
    tmdb_id = int(parts[3])

    movie_data = get_movie_details(tmdb_id)
    if not movie_data:
        await bot.answer_callback_query(callback_query.id, "No se pudo obtener la información de la película. No se puede notificar.", show_alert=True)
        return

    try:
        await bot.send_message(
            user_request_id,
            f"✅ La película que solicitaste, '{movie_data.get('title')}', ya está disponible en el canal. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Haz clic aquí para verla.</a>",
            parse_mode=ParseMode.HTML
        )
        await bot.answer_callback_query(callback_query.id, "✅ Usuario notificado con éxito.", show_alert=True)
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=f"✅ Usuario notificado sobre '{movie_data.get('title')}'."
        )
    except Exception as e:
        await bot.answer_callback_query(callback_query.id, "Ocurrió un error al notificar al usuario.", show_alert=True)
        logging.error(f"Error al notificar al usuario {user_request_id}: {e}")

# Funciones de publicación automática
async def auto_post_task():
    """
    Tarea asincrónica que publica películas automáticamente en el canal.
    """
    while True:
        try:
            # 1. Publicar desde la cola de posts programados
            if not scheduled_posts.empty():
                logging.info("Procesando posts programados...")
                movie_info, delay_minutes = await scheduled_posts.get()
                await asyncio.sleep(delay_minutes * 60)
                movie_data = get_movie_details(movie_info.get("id"))
                if movie_data:
                    await delete_old_post(movie_info.get("id"))
                    await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"))
                else:
                    logging.error(f"No se pudo obtener la información de la película programada con ID {movie_info.get('id')}.")
                scheduled_posts.task_done()
                continue  # Volver al inicio del bucle para revisar si hay más posts programados

            # 2. Publicar automáticamente de forma periódica
            now = datetime.datetime.now()
            # Intervalo en horas entre posts automáticos
            interval_hours = 24 / AUTO_POST_COUNT
            
            # Hora de la última publicación automática
            last_auto_post_time = admin_data.get("last_auto_post_time", None)

            if last_auto_post_time is None or (now - last_auto_post_time).total_seconds() >= interval_hours * 3600:
                logging.info("Hora de una nueva publicación automática.")
                available_movies = [m for m in movies_db.values() if m.get("id") not in [p.get("id") for p in recent_posts]]
                
                if available_movies:
                    chosen_movie = random.choice(available_movies)
                    movie_data = get_movie_details(chosen_movie.get("id"))
                    if movie_data:
                        await delete_old_post(chosen_movie.get("id"))
                        success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, chosen_movie.get("link"))
                        if success:
                            admin_data["last_auto_post_time"] = now
                            recent_posts.append(chosen_movie)
                            logging.info(f"Publicación automática de '{chosen_movie.get('names')[0]}' completada.")
                    else:
                        logging.error(f"No se pudo obtener la información de la película aleatoria con ID {chosen_movie.get('id')}.")
                else:
                    logging.warning("No hay películas disponibles para publicación automática.")

        except Exception as e:
            logging.error(f"Error en la tarea de publicación automática: {e}")

        # Esperar un minuto antes de la siguiente revisión
        await asyncio.sleep(60)

async def main():
    keep_alive()
    load_movies_db()
    # Iniciar la tarea de publicación automática
    asyncio.create_task(auto_post_task())
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
