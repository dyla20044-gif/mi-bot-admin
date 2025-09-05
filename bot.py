import asyncio
import logging
import json
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
from webserver import keep_alive # Importa la función keep_alive

# 1. Configuración de credenciales y constantes
TELEGRAM_BOT_TOKEN = "8470210495:AAHSMzLftU9Gqrl9sNEEp_IUo7WYFSXH1HU"
TMDB_API_KEY = "5eb8461b85d0d88c46d77cfe5436291f"
TELEGRAM_CHANNEL_ID = -1002139779491
ADMIN_ID = 6115976248
BASE_TMDB_URL = "https://api.themoviedb.org/3"
POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"
MOVIES_DB_FILE = "movies.json"

# Almacenamiento de posts programados y posts recientes
scheduled_posts = asyncio.Queue()
recent_posts = deque(maxlen=20)

# Almacenamiento temporal para solicitudes de usuarios
user_requests = {}

# Configuración del logging
logging.basicConfig(level=logging.INFO)

# 2. Inicialización del bot, dispatcher y la "base de datos"
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
movies_db = {}
AUTO_POST_COUNT = 4

# Estados para la máquina de estados de aiogram
class MovieUploadStates(StatesGroup):
    waiting_for_movie_info = State()

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

def get_movie_id_by_title(title):
    url = f"{BASE_TMDB_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "es-ES"}
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
                [types.KeyboardButton(text="➕ Agregar película"), types.KeyboardButton(text="📋 Ver películas")],
                [types.KeyboardButton(text="⚙️ Configuración auto-publicación")]
            ],
            resize_keyboard=True
        )
        await message.reply(
            "¡Hola, Administrador! Elige una opción:\n\n"
            "**Opciones de Administrador:**\n"
            "➕ **Agregar película:** Agrega una nueva película a la base de datos.\n"
            "📋 **Ver películas:** Muestra una lista de todas las películas que has agregado.\n"
            "⚙️ **Configuración auto-publicación:** Cambia la cantidad de publicaciones automáticas al día.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📽️ Pedir una película", callback_data="ask_for_movie")]
        ])
        await message.reply(
            "¡Hola! Soy un bot que te ayuda a encontrar tus películas favoritas.\n\n"
            "**¿Qué puedo hacer?**\n"
            "🎬 **Buscar películas:** Haz clic en el botón de abajo para solicitar una película. Si está en mi base de datos, la publicaré al instante en el canal.\n"
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
        "Título Principal | Nombre_1, Nombre_2, Nombre_3 | Enlace_de_la_película"
    )
    await state.set_state(MovieUploadStates.waiting_for_movie_info)

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
        await message.reply("Formato incorrecto. Por favor, usa el formato: Título Principal | Nombres | Enlace")
        return

    main_title = parts[0].strip()
    names_str = parts[1].strip()
    movie_link = parts[2].strip()
    
    names = [name.strip() for name in names_str.split(',')]

    await message.reply(f"Buscando '{main_title}' en TMDB...")
    
    movie_id = get_movie_id_by_title(main_title)
    if not movie_id:
        await message.reply(
            f"No se pudo encontrar la película '{main_title}' en TMDB. "
            "Por favor, asegúrate de escribir el título correctamente."
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
        text="Por favor, envía la información de la siguiente película en el formato: Título Principal | Nombres | Enlace"
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
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📌 Publicar ahora esta película", callback_data=f"publish_requested_{movie_title}")]
        ])
        
        user_requests[movie_title.lower()] = message.from_user.id
        
        await bot.send_message(ADMIN_ID, f"El usuario {message.from_user.full_name} (@{message.from_user.username}) ha solicitado la película: <b>{movie_title}</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        
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

@dp.callback_query(F.data.startswith("publish_requested_"))
async def publish_requested_movie(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await bot.answer_callback_query(callback_query.id, "No tienes permiso para esta acción.")
        return

    requested_title = callback_query.data.split("publish_requested_")[1]
    
    main_title, movie_info = find_movie_in_db(requested_title)
    if not movie_info:
        await bot.send_message(callback_query.message.chat.id, f"La película '{requested_title}' no se encontró en la base de datos. Agrégala primero.")
        return

    movie_id = movie_info.get("id")
    movie_link = movie_info.get("link")

    movie_data = get_movie_details(movie_id)
    if not movie_data:
        await bot.send_message(callback_query.message.chat.id, f"Error al obtener los detalles de '{requested_title}' desde TMDB. No se puede publicar.")
        return
    
    await delete_old_post(movie_data.get("id"))
    
    success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_link)

    if success:
        await bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text=f"✅ Película '{requested_title}' publicada con éxito."
        )
        
        user_id = user_requests.get(requested_title.lower())
        if user_id:
            await bot.send_message(user_id, f"✅ Tu película <b>{requested_title}</b> ha sido publicada. <a href='https://t.me/+C8xLlSwkqSc3ZGU5'>Puedes verla aquí.</a>", parse_mode=ParseMode.HTML)
            del user_requests[requested_title.lower()]

    else:
        await bot.send_message(callback_query.message.chat.id, f"Ocurrió un error al publicar '{requested_title}'.")

# Tarea de publicación automática
async def automatic_movie_post():
    while True:
        if not movies_db:
            logging.warning("No hay películas en la base de datos para la publicación automática.")
            await asyncio.sleep(3600)
            continue

        interval_seconds = 86400 / AUTO_POST_COUNT # 86400 segundos en un día
        
        movie_keys = list(movies_db.keys())
        
        # Filtra las películas que no se han publicado recientemente
        available_movies = [key for key in movie_keys if key not in recent_posts]
        if not available_movies:
            available_movies = movie_keys # Resetea la lista si se han publicado todas

        movies_to_post = random.sample(available_movies, min(AUTO_POST_COUNT, len(available_movies)))
        
        for key in movies_to_post:
            movie_info = movies_db.get(key)
            movie_id = movie_info.get("id")
            movie_link = movie_info.get("link")

            if not movie_id or not movie_link:
                logging.error(f"Película '{key}' en la base de datos no tiene ID o enlace. Omitiendo.")
                continue

            movie_data = get_movie_details(movie_id)
            if not movie_data:
                logging.error(f"Error al obtener los detalles de '{key}' desde TMDB. Omitiendo.")
                continue
            
            await delete_old_post(movie_data.get("id"))
            
            success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_link)
            if success:
                logging.info(f"Publicación automática de película '{key}' enviada.")
                recent_posts.append(key)
            
            await asyncio.sleep(interval_seconds)

# Tarea para publicar mensajes programados
async def scheduled_posts_task():
    while True:
        if not scheduled_posts.empty():
            movie_info, delay_minutes = await scheduled_posts.get()
            logging.info(f"Programando publicación para '{movie_info.get('names', [''])[0]}' en {delay_minutes} minutos.")
            await asyncio.sleep(delay_minutes * 60)
            
            movie_data = get_movie_details(movie_info.get("id"))
            if movie_data:
                await delete_old_post(movie_data.get("id"))
                success, _ = await send_movie_post(TELEGRAM_CHANNEL_ID, movie_data, movie_info.get("link"))
                if success:
                    logging.info(f"Publicación programada de '{movie_info.get('names', [''])[0]}' enviada.")
                    recent_posts.append(list(movies_db.keys())[list(movies_db.values()).index(movie_info)])
                else:
                    logging.error(f"Error al enviar la publicación programada de '{movie_info.get('names', [''])[0]}'.")
            else:
                logging.error(f"Error al obtener los detalles de la película programada con ID {movie_info.get('id')}.")
        
        await asyncio.sleep(1)

# 8. Inicio del bot
async def main():
    load_movies_db()
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text="Bot iniciado ✅")
    except Exception as e:
        logging.error(f"No se pudo enviar el mensaje de inicio al canal: {e}")
    
    asyncio.create_task(automatic_movie_post())
    asyncio.create_task(scheduled_posts_task())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    keep_alive()
    asyncio.run(main())
