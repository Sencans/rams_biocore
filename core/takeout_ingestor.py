"""Ingestor de Google Takeout → ``perfil_takeout.json`` (recuerdo heredado de hábitos digitales).

Qué hace: lee **dentro** de los zips de Google Takeout (``zipfile``, en streaming y sin
descomprimir nada a disco) y extrae **solo patrones agregados del propio usuario**, como
:mod:`core.wsp_ingestor` hace con WhatsApp. Es la segunda fuente del recuerdo heredado de
Amadeus: lo conoce por datos, no por haberlo vivido.

Qué se guarda (``data/john/perfil_takeout.json``, privado, 0o600):

- **Intereses** ponderados por categorías de un léxico curado y **palabras clave**: solo términos
  de un **vocabulario cerrado** (el léxico de intereses y una lista de temas neutros), nunca una
  palabra arbitraria de un título (así no se cuelan nombres de terceros, apodos, *handles* ni
  lugares), con frecuencia mínima, de los títulos del historial de Chrome, los favoritos, los
  nombres de archivos de Drive (solo el índice del zip) y los nombres de Gems de Gemini.
- **Plataformas** por dominio registrable (``youtube.com``; nunca rutas, URLs ni parámetros), solo
  de una **lista de sitios conocidos** y no sensibles; los demás se cuentan sin nombrarlos. Igual
  con los servicios de Google más usados y los sitios guardados.
- **Ritmo** (horas y días de la semana) por fuente: navegación, correo enviado y fotos, en la
  zona horaria del usuario (``Settings.zona_horaria``).
- **Estilo** de **sus** correos enviados a terceros (longitud, saludos, despedidas, muletillas,
  formalidad, emojis): solo con ≥ 20 correos de ≥ 20 palabras propias y la confianza marcada.
- **Servicios que le escriben** por dominio de la misma lista de sitios conocidos (nunca personas).
- **Herramientas**: extensiones de Chrome conocidas o con un nombre de vocabulario cerrado.
- **Fotos**: solo la fecha de toma (ritmo y año); **Drive**: tipos de archivo y palabras clave.
- Su **nombre de pila** de la Cuenta de Google (solo se usa para excluirlo de todo lo demás; no
  entra en el resumen).

Qué **nunca** se lee ni se guarda (decisiones de privacidad obligatorias):

- ``Direcciones y más.json`` (autocompletar), Contactos, ``Diccionario.csv``, ``ChangeHistory``,
  la configuración y el dispositivo de Chrome, las direcciones bloqueadas del correo, las
  acciones programadas de Gemini y **ningún** archivo de Drive (tampoco los zips anidados).
- Ubicaciones (``geoData``, títulos de Google Maps, Waze, Uber…), caras/personas, descripciones ni
  títulos de las fotos; fotos en la papelera o de álbumes compartidos.
- Títulos de correo y webmail, mensajería, perfiles sociales, documentos de Docs/Drive y
  repositorios (``dueño/repo``): pueden ser nombres o asuntos de otras personas.
- Cuerpos de correos ajenos, Spam, Papelera ni borradores; direcciones de correo, nombres de
  terceros, números, URLs completas ni títulos literales. Los nombres y direcciones de quienes
  aparecen en las cabeceras de los correos se usan **solo en memoria**, durante la ingesta, para
  excluirlos de todo lo demás (también el apellido del propio usuario).
- Categorías especiales (salud, sexualidad, religión, política, finanzas personales y legal):
  filtradas por defecto con un léxico curado (palabras, raíces y frases; ``incluir_sensibles=True``
  para incluirlas). El léxico **no es exhaustivo**: por eso el resumen se revisa antes de usarse.

Todo es local: nada de red ni de LLM. El resultado se revisa antes de usarse
(:func:`resumen_perfil_takeout`) y se pueden quitar temas (:func:`quitar_temas`, ``--quitar``).
Las exclusiones viven aparte, en ``takeout_exclusiones.json`` (junto al perfil), como HMAC con
una clave local aleatoria: es **ofuscación** (el texto del pedido no queda a la vista), no
cifrado frente a quien tenga acceso a toda la carpeta ``data/``. Olvidar el perfil las conserva.

Uso (CLI)::

    python -m core.takeout_ingestor takeout-*.zip [--salida RUTA] [--incluir-sensibles]
                                    [--mostrar] [--quitar "tema1,tema2"] [--zona America/Bogota]
                                    [--reemplazar]

    python -m core.takeout_ingestor --mostrar            # muestra el perfil ya guardado
    python -m core.takeout_ingestor --quitar "anime"      # quita temas del perfil ya guardado

Dependencias: solo la biblioteca estándar y :mod:`core.wsp_ingestor` (utilidades de estilo,
muletillas y vocabulario). ``core.config`` se importa únicamente dentro de :func:`main`.

Degradación: un zip ilegible o duplicado se ignora; una fuente rota (JSON inválido, HTML raro,
mbox truncado) queda con ``estado: "error"`` y no tumba a las demás.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import statistics
import time
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from email import message_from_bytes
from email import policy as politica_correo
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import IO, Any, Final
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.wsp_ingestor import (
    NOMBRES_COMUNES_ES,
    STOPWORDS_ES,
    _chmod_privado,
    _decimal,
    _emojis,
    _empieza_con_mayuscula,
    _es_texto_seguro,
    _franja,
    _imprimir,
    _item,
    _real,
    _sin_tildes,
    _texto_tokenizable,
    extraer_muletillas,
)

__all__ = [
    "VERSION_PERFIL_TAKEOUT",
    "VERSION_MINIMA_EN_USO",
    "TIPO_PERFIL",
    "Exclusiones",
    "construir_perfil_takeout",
    "dominio_registrable",
    "guardar_perfil",
    "guardar_perfil_takeout",
    "cargar_perfil_takeout",
    "perfil_vigente",
    "resumen_perfil_takeout",
    "quitar_temas",
    "ruta_exclusiones",
    "cargar_exclusiones",
    "guardar_exclusiones",
    "main",
]

log = logging.getLogger("rams.takeout")

# v2: vocabulario cerrado para las palabras clave, lista de sitios conocidos, exclusiones fuera del
# perfil (``takeout_exclusiones.json``) y umbrales de estilo de ESQUEMA §4.5. Un perfil v1 se sigue
# pudiendo leer (``--mostrar``), pero main no lo usa: se generó con filtros de privacidad más débiles.
VERSION_PERFIL_TAKEOUT: Final = 2
VERSION_MINIMA_EN_USO: Final = 2
TIPO_PERFIL: Final = "perfil_takeout"
TIPO_EXCLUSIONES: Final = "exclusiones_takeout"
NOMBRE_EXCLUSIONES: Final = "takeout_exclusiones.json"
MAX_RESUMEN: Final = 900

# --- Topes de lectura (defensa ante zips raros; el Takeout real queda muy por debajo) -------
MAX_JSON_BYTES: Final = 256 * 1024 * 1024       # Historial.json real: 18,6 MB
MAX_HTML_BYTES: Final = 64 * 1024 * 1024        # Favoritos y SubscriberInfo (196 KB)
MAX_SIDECAR_BYTES: Final = 1024 * 1024          # .json de metadatos de una foto
MAX_CABECERA_CORREO: Final = 512 * 1024         # cabeceras de un mensaje (máx. real: 269 líneas)
MAX_CUERPO_ENVIADO: Final = 2 * 1024 * 1024     # solo de los enviados por el usuario
MAX_TEXTO_ENVIADO: Final = 20_000               # caracteres de texto propio por correo
MAX_LINEA_MBOX: Final = 1024 * 1024

# --- Umbrales de privacidad y de evidencia ------------------------------------------------
MIN_VISITAS_DOMINIO: Final = 5          # un dominio entra en "plataformas" con ≥ 5 visitas…
MIN_DIAS_DOMINIO: Final = 2             # …repartidas en ≥ 2 días
MIN_FAVORITOS_DOMINIO: Final = 2
MIN_DOCS_TITULOS: Final = 3             # palabra clave de Chrome: ≥ 3 títulos distintos…
MIN_DIAS_TITULOS: Final = 2             # …vistos en ≥ 2 días distintos
MIN_DOCS_FAVORITOS: Final = 3
MIN_DOCS_DRIVE: Final = 3
MIN_DOCS_MULTIFUENTE: Final = 3         # o ≥ 3 documentos repartidos en ≥ 2 fuentes
MIN_EVIDENCIA_CATEGORIA: Final = 3
MIN_PESO_CATEGORIA: Final = 0.05
MIN_MENSAJES_SERVICIO: Final = 3
# Estilo de correo (ESQUEMA §4.5): ≥ 20 correos a terceros con ≥ 20 palabras propias (sin citas,
# reenvíos ni firma); confianza «baja» por debajo de 50 y «media» por debajo de 150.
MIN_CORREOS_ESTILO: Final = 20
MIN_PALABRAS_ESTILO: Final = 20
CORREOS_CONFIANZA_MEDIA: Final = 50
CORREOS_CONFIANZA_ALTA: Final = 150
MIN_MULETILLA_CORREO: Final = 3
MAX_DIGITOS_ETIQUETA: Final = 2         # una extensión o un Gem con ≥ 3 dígitos se descarta
MIN_EVENTOS_RITMO: Final = 20
MAX_INTERESES: Final = 12
MAX_PALABRAS: Final = 25
MAX_PLATAFORMAS: Final = 15
MAX_SITIOS_GUARDADOS: Final = 10
MAX_SERVICIOS_CORREO: Final = 15
MAX_EXTENSIONES: Final = 15
MAX_GEMS: Final = 10
MAX_ETIQUETA: Final = 60

_DIAS: Final = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_DIAS_PLURAL: Final = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábados", "domingos")
_FRANJA_TEXTO: Final = {"madrugada": "madrugada", "manana": "mañana", "tarde": "tarde", "noche": "noche"}

Progreso = Callable[[str], None]

# ---------------------------------------------------------------------------
# Rutas del Takeout
# ---------------------------------------------------------------------------

# 2.º componente de ``Takeout/<Producto>/…`` (normalizado: sin tildes, minúsculas y con el
# espacio duro U+00A0 de "Google Fotos" convertido en espacio) → clave de la fuente.
_PRODUCTOS: Final[Mapping[str, str]] = {
    "chrome": "chrome",
    "contactos": "contactos", "contacts": "contactos",
    "cuenta de google": "cuenta", "google account": "cuenta",
    "gemini": "gemini", "gemini apps": "gemini",
    "google fotos": "fotos", "google photos": "fotos",
    "drive": "drive", "google drive": "drive",
    "correo electronico": "correo", "mail": "correo", "gmail": "correo",
}

# Archivos de Chrome que sí se leen (nombre base normalizado → clave). Todo lo demás de Chrome
# (autocompletar, diccionario, configuración, dispositivo…) se excluye sin abrirse.
_CHROME_PERMITIDOS: Final[Mapping[str, str]] = {
    "historial.json": "historial", "browserhistory.json": "historial", "history.json": "historial",
    "favoritos.html": "favoritos", "bookmarks.html": "favoritos",
    "lista de lectura.html": "favoritos", "reading list.html": "favoritos", "readinglist.html": "favoritos",
    "extensiones.json": "extensiones", "extensions.json": "extensiones",
}
_GEMINI_PERMITIDOS: Final = frozenset({"gemini_gems_data.html", "gems.html"})
_CARPETAS_PAPELERA: Final = frozenset({"papelera", "trash", "bin", "eliminados", "deleted"})
_RE_PARTE: Final = re.compile(r"^takeout-(?P<sello>[0-9a-z]+)-(?P<grupo>\d+)-(?P<num>\d{3})(?: \(\d+\))?\.zip$", re.I)

# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------


def _terminos(texto: str) -> tuple[str, ...]:
    """Términos separados por comas, normalizados (minúsculas, sin tildes, espacios simples)."""
    return tuple(t for t in (" ".join(_sin_tildes(p).split()) for p in texto.split(",")) if t)


# Intereses (no sensibles). Términos de una palabra o frases; se comparan con el texto
# normalizado en palabra completa. Evita términos ambiguos (p. ej. "switch", "salsa").
_INTERESES: Final[Mapping[str, tuple[str, ...]]] = {
    "programación y desarrollo": _terminos(
        "python, javascript, typescript, java script, rust, golang, php, html, css, react, reactjs, nextjs, "
        "nodejs, node js, npm, django, flask, fastapi, docker, kubernetes, github, gitlab, git, stackoverflow, "
        "stack overflow, api, apis, backend, frontend, fullstack, programacion, programar, programador, "
        "programming, programmer, codigo, coding, developer, desarrollador, software, linux, ubuntu, debian, "
        "hyprland, vscode, visual studio, sql, postgres, postgresql, mysql, sqlite, mongodb, prisma, electron, "
        "vercel, pip, pytest, tailwind, webpack, vite, laravel, wordpress, devops, nginx, vps, ssh, terminal, "
        "bash, powershell, script, scripts, regex, algoritmo, algoritmos, algorithm, compilador, compiler, "
        "debug, deploy, despliegue, hosting, servidor, servidores, server, cloudpanel, framework, libreria, "
        "library, repositorio, repository, commit, json, yaml, markdown, base de datos, database, "
        "next js, three js, threejs, vue, vuejs, angular, svelte, kotlin, flutter, android studio, redis, "
        "graphql, supabase, firebase, websocket, websockets, asyncio, numpy, pandas, opencv, ffmpeg, pm2, "
        "cloudflare, cpanel, webhook, webhooks, oauth, microservicios, testing, pruebas unitarias, refactor, "
        "refactorizar, responsive, netlify, heroku, aws, azure, terraform, ansible, github actions, "
        "pull request, scraping, web scraping, neovim, wsl, tauri, sdk, dockerfile, nodejs, typescript"),
    "inteligencia artificial": _terminos(
        "ia, ai, inteligencia artificial, artificial intelligence, chatgpt, openai, gemini, claude, anthropic, "
        "llm, llms, gpt, ollama, huggingface, hugging face, stable diffusion, midjourney, machine learning, "
        "deep learning, red neuronal, redes neuronales, neural network, modelo de lenguaje, prompt, prompts, "
        "agente, agentes, agent, agents, copilot, langchain, transformer, transformers, embeddings, rag, "
        "puter, elevenlabs, mcp, chatbot, pytorch, tensorflow, deepseek, comfyui, fine tuning, notebooklm, "
        "ai studio, texto a voz, text to speech, tts, prompt engineering, openrouter, perplexity"),
    "anime, manga y VTubers": _terminos(
        "anime, animes, manga, mangas, crunchyroll, otaku, steins gate, steins, kurisu, makise, okabe, "
        "konosuba, megumin, waifu, isekai, shonen, seinen, vtuber, vtubers, hololive, nijisanji, vrm, vroid, "
        "live2d, myanimelist, animeflv, cosplay, light novel, novela ligera, doujin, mmd, amadeus, "
        "one piece, dragon ball, demon slayer, kimetsu, jujutsu kaisen, attack on titan, shingeki, evangelion, "
        "gundam, ghibli, studio ghibli, manhwa, webtoon, visual novel, novela visual, mecha, seiyuu, vrchat"),
    "videojuegos": _terminos(
        "videojuego, videojuegos, juego, juegos, game, games, gaming, gamer, steam, playstation, xbox, "
        "nintendo, minecraft, fortnite, valorant, league of legends, genshin, roblox, twitch, epic games, "
        "speedrun, emulador, emulator, retroarch, zelda, pokemon, elden ring, dark souls, rpg, mmorpg, "
        "shooter, indie game, mods, modding, gameplay, nintendo switch, ps5, ps4, steam deck, hollow knight, "
        "terraria, stardew valley, counter strike, dota, overwatch, apex legends, call of duty, warzone, "
        "unity, godot, unreal engine, emulacion"),
    "música": _terminos(
        "musica, music, spotify, cancion, canciones, song, songs, album, playlist, letra, letras, lyrics, "
        "guitarra, guitar, piano, concierto, concert, rap, rock, reggaeton, vallenato, karaoke, acordes, "
        "chords, beat, beats, fl studio, ableton, soundcloud, cantante, banda sonora, soundtrack, ost, "
        "metal, jazz, cumbia, bachata, kpop, jpop, vocaloid, lofi, synthwave, produccion musical, violin, "
        "ukulele, partitura, partituras, midi"),
    "cine y series": _terminos(
        "pelicula, peliculas, movie, movies, film, films, serie, series, netflix, disney, hbo, prime video, "
        "cine, cinema, trailer, temporada, season, episodio, episode, imdb, documental, documentary, "
        "anime movie, estreno, estrenos, marvel, star wars, star trek, pixar, ciencia ficcion, sci fi, "
        "terror, horror, thriller, dorama, kdrama"),
    "video y streaming": _terminos(
        "youtube, youtuber, stream, streaming, streamer, obs, edicion de video, editar video, premiere, "
        "davinci, davinci resolve, capcut, shorts, tiktok, reels, podcast, podcasts, vlog, streamlabs, "
        "miniatura, miniaturas, thumbnail"),
    "ciencia": _terminos(
        "ciencia, science, fisica, physics, quimica, chemistry, biologia, biology, neurociencia, neuroscience, "
        "cerebro, brain, astronomia, astronomy, nasa, universo, universe, cuantica, quantum, matematicas, "
        "math, mathematics, investigacion cientifica, research, paper, papers, arxiv, conectoma, connectome, "
        "drosophila, flywire, neurona, neuronas, neuron, neurons, evolucion, genetica, genetics, "
        "agujero negro, agujeros negros, cosmos, spacex, cohete, cohetes, telescopio, dinosaurio, dinosaurios, "
        "experimento, experimentos, divulgacion cientifica, matematica"),
    "tecnología y hardware": _terminos(
        "tecnologia, technology, tech, hardware, computador, computadora, laptop, portatil, gpu, cpu, nvidia, "
        "amd, intel, ssd, monitor, teclado, keyboard, mouse, android, iphone, samsung, xiaomi, celular, "
        "smartphone, router, arduino, raspberry, raspberry pi, impresora, printer, windows, microsoft, apple, "
        "macos, drivers, bios, overclock, electronica, electronics, mini pc, homelab, memoria ram, procesador, "
        "tarjeta grafica, placa base, motherboard, ryzen, rtx, radeon, geforce, audifonos, auriculares, "
        "headphones, smartwatch, tablet, ipad, macbook, pc gamer"),
    "diseño y creatividad": _terminos(
        "diseno, design, figma, photoshop, illustrator, canva, blender, render, ilustracion, illustration, "
        "dibujo, drawing, arte, art, artwork, pixel art, logo, logos, tipografia, typography, font, fonts, ui, "
        "ux, paleta, palette, animacion, animation, modelado 3d, sketch, wallpaper, wallpapers, procreate, "
        "krita, gimp, inkscape, after effects, motion graphics"),
    "negocios y emprendimiento": _terminos(
        "negocio, negocios, business, emprendimiento, emprender, startup, startups, marketing, ventas, sales, "
        "ecommerce, e commerce, tienda online, shopify, clientes, seo, publicidad, advertising, empresa, "
        "branding, mercadeo, freelance, freelancer, landing page, crm, saas, emprendedor, inventario, "
        "tienda virtual, dropshipping, afiliados, copywriting, community manager, pyme, pymes, proveedor, "
        "proveedores"),
    "inmobiliario y arriendos": _terminos(
        "inmobiliaria, inmobiliario, finca raiz, arriendo, arriendos, alquiler, alquileres, alquile, "
        "apartamento, apartamentos, real estate, propiedad horizontal, metrocuadrado, fincaraiz, inmuebles, "
        "arrendamiento, arrendatario, arrendador, propietario, propietarios, inquilino, inquilinos"),
    "educación e idiomas": _terminos(
        "curso, cursos, course, courses, tutorial, tutoriales, tutorials, aprender, learn, learning, clase, "
        "clases, universidad, university, udemy, coursera, platzi, documentacion, documentation, guia, guide, "
        "ingles, english, idioma, idiomas, duolingo, japones, japanese, certificacion, bootcamp"),
    "noticias y actualidad": _terminos(
        "noticias, noticia, news, periodico, actualidad, articulo, articulos, reportaje, breaking news"),
    "deportes": _terminos(
        "futbol, football, soccer, deporte, deportes, sport, sports, nba, basketball, baloncesto, tenis, tennis, "
        "ciclismo, gym, gimnasio, entrenamiento, workout, running, champions, mundial, gol, goles, "
        "seleccion colombia, formula 1, f1, ajedrez, chess"),
    "cocina y comida": _terminos(
        "receta, recetas, recipe, recipes, cocina, cocinar, cooking, comida, food, restaurante, restaurant, "
        "postre, postres, cafe, coffee, pizza, hamburguesa, rappi, domicilios, reposteria, arepa, arepas, "
        "empanada, empanadas, bandeja paisa, ajiaco, sancocho, tamal, tamales, bunuelo, bunuelos, pandebono, "
        "torta, tortas, pasta, sushi, ramen, parrilla, asado, air fryer, freidora de aire, panaderia, "
        "hornear, galletas"),
    "viajes": _terminos(
        "viaje, viajes, travel, vuelo, vuelos, flight, flights, hotel, hoteles, airbnb, booking, turismo, "
        "tourism, playa, avianca, aeropuerto, airport, mochilero"),
    "compras en línea": _terminos(
        "mercadolibre, mercado libre, amazon, aliexpress, temu, shein, ebay, compras, comprar, oferta, ofertas, "
        "descuento, descuentos, cupon, cupones, black friday, cyber monday, envio gratis"),
    "redes sociales": _terminos(
        "facebook, instagram, twitter, reddit, discord, telegram, whatsapp, linkedin, pinterest, threads, "
        "tumblr, bluesky, mastodon, redes sociales, social media"),
    "productividad y ofimática": _terminos(
        "productividad, productivity, notion, trello, asana, todoist, calendario, agenda, excel, sheets, "
        "spreadsheet, hoja de calculo, word, powerpoint, plantilla, plantillas, template, templates, zapier, "
        "n8n, macro, macros, workflow, automatizacion, automation, pdf, ofimatica, obsidian"),
    "fotografía": _terminos(
        "fotografia, photography, camara, camera, lightroom, lente, lens, retrato, portrait, fotografo"),
    "motor": _terminos(
        "carro, carros, auto, autos, car, cars, moto, motos, motorcycle, vehiculo, vehiculos, mecanica, tesla"),
    "hogar y mascotas": _terminos(
        "hogar, muebles, decoracion, bricolaje, diy, plantas, jardin, jardineria, mascota, mascotas, perro, "
        "perros, gato, gatos, cachorro"),
}

# Categorías especiales (filtradas por defecto). Un documento que nombra cualquiera de estos
# términos (o empieza por una de las raíces de ``_PREFIJOS_SENSIBLES``) se descarta entero: sus
# otras palabras pueden dar el contexto sensible. Las palabras ambiguas («demanda», «herencia»,
# «política», «doctor», «god») solo cuentan dentro de una frase que les da el sentido sensible
# («demanda laboral», «política colombiana»), para no confundir «oferta y demanda», «herencia en
# Python», «política de privacidad», «Doctor Who» ni «God of War». El léxico no es exhaustivo.
_SENSIBLES: Final[Mapping[str, tuple[str, ...]]] = {
    "salud": _terminos(
        "salud, health, medico, medica, medicos, clinica, clinicas, hospital, hospitales, eps, ips, "
        "sintoma, sintomas, symptom, symptoms, enfermedad, enfermedades, disease, diagnostico, diagnosis, "
        "medicamento, medicamentos, medicine, medication, pastilla, pastillas, farmacia, farmacias, pharmacy, "
        "drogueria, droguerias, terapia, therapy, psicologo, psicologa, psicologia, psychologist, psychology, "
        "psiquiatra, psychiatrist, depresion, depression, ansiedad, anxiety, cancer, diabetes, embarazo, "
        "embarazada, pregnancy, pregnant, vih, hiv, sida, covid, vacuna, vacunas, vaccine, dolor, cita medica, "
        "examen de sangre, examen medico, examenes medicos, examenes de laboratorio, examenes laboratorio, "
        "resultados de laboratorio, laboratorio clinico, historia clinica, incapacidad medica, colesterol, "
        "presion arterial, migrana, insomnio, suicidio, suicide, adiccion, addiction, rehabilitacion, sanitas, "
        "compensar, famisanar, colsanitas, nueva eps, salud total, sura, coosalud, medimas, dieta, diet, "
        "nutricion, nutricionista, odontologo, odontologa, dentista, dentist, ginecologo, ginecologa, urologo, "
        # salud mental y neurodesarrollo
        "tdah, adhd, bipolar, bipolaridad, trastorno, trastornos, disorder, autismo, autista, asperger, "
        "esquizofrenia, psicosis, ocd, ptsd, autolesion, anorexia, bulimia, psicoterapia, metilfenidato, "
        "ritalin, sertralina, fluoxetina, escitalopram, clonazepam, alprazolam, quetiapina, risperidona, "
        "lorazepam, antidepresivo, antidepresivos, ansiolitico, ansioliticos, "
        # oncología, pruebas y especialidades
        "biopsia, oncologia, oncologo, quimioterapia, radioterapia, tumor, tumores, metastasis, leucemia, "
        "ecografia, radiografia, tomografia, mamografia, resonancia magnetica, citologia, endoscopia, "
        "colonoscopia, cirugia, urgencias, hemograma, tiroides, hipotiroidismo, hipertension, epilepsia, "
        "alzheimer, parkinson, "
        # salud sexual y reproductiva
        "fertilidad, infertilidad, ovulacion, menstruacion, menstrual, anticonceptivo, anticonceptivos, "
        "anticonceptiva, aborto, abortar, interrupcion voluntaria del embarazo, profamilia, papiloma, vph, "
        "herpes, sifilis, gonorrea, clamidia, "
        # farmacias e IPS de Colombia
        "cruz verde, cruzverde, farmatodo, locatel, larebaja, drogas la rebaja, drogueria la rebaja, "
        "colsubsidio, cafam"),
    "sexualidad": _terminos(
        "sexo, sex, sexual, sexuales, sexy, porn, porno, pornografia, xxx, nsfw, hentai, onlyfans, fansly, "
        "erotico, erotica, erotic, desnudo, desnuda, desnudos, nude, nudes, lgbt, lgbtq, lgbtiq, gay, lesbiana, "
        "lesbian, bisexual, transgenero, transgender, transexual, travesti, homosexual, homosexualidad, "
        "pansexual, asexual, queer, mujer trans, hombre trans, identidad de genero, orientacion sexual, "
        "tinder, grindr, bumble, badoo, condon, condones, condom, xvideos, xnxx, pornhub, xhamster, chaturbate, "
        "stripchat, rule34, rule 34, fetiche, fetish, bdsm, milf, camgirl, escort, escorts, prepago, prepagos, "
        "stripper, striptease, swinger, swingers, sexting, masturbacion, orgasmo, vibrador, juguetes sexuales, "
        "sex shop, sexshop, erome, missav, f95zone, nhentai, jav, r18, citas en linea, dating"),
    "religión": _terminos(
        "religion, religiones, religioso, religiosa, iglesia, church, dios, biblia, bible, cristo, christ, "
        "catolico, catolica, catholic, cristiano, cristiana, evangelico, evangelica, misa, oracion, oraciones, "
        "prayer, rezar, pray, islam, musulman, muslim, judio, judia, jewish, budismo, buddhism, ateo, atheist, "
        "ateismo, jehova, testigos de jehova, sacerdote, vaticano, versiculo, salmo, salmos, evangelio, gospel, "
        "coran, quran, torah, espiritual, espiritualidad, parroquia, word of god, palabra de dios"),
    "política": _terminos(
        "politics, political, politico, politicos, politica colombiana, politica nacional, "
        "politica internacional, elecciones, election, elections, electoral, votar, voto, votacion, votaciones, "
        "vote, presidente, presidenta, president, congreso, congress, senado, senate, senador, senadora, "
        "candidato, candidata, candidatos, partido politico, izquierda politica, derecha politica, petro, "
        "gustavo petro, uribe, alvaro uribe, uribismo, uribista, petrismo, petrista, trump, biden, milei, "
        "bukele, nicolas maduro, chavismo, chavista, comunismo, comunista, socialismo, socialista, fascismo, "
        "fascista, protesta, protestas, paro nacional, referendo, registraduria, alcaldia, gobernacion, "
        "ministro, ministra, campana electoral, pacto historico, colombia humana, centro democratico, "
        "cambio radical, partido liberal, partido conservador, alianza verde, polo democratico, "
        "partido de la u, reforma pensional, reforma tributaria, reforma laboral"),
    "finanzas personales": _terminos(
        "banco, bancos, bank, banking, bancolombia, davivienda, nequi, daviplata, bbva, colpatria, scotiabank, "
        "nubank, lulo bank, lulobank, credito, creditos, credit, prestamo, prestamos, loan, loans, deuda, "
        "deudas, debt, tarjeta de credito, credit card, cuenta de ahorros, extracto, saldo, transferencia, pse, "
        "paypal, payoneer, binance, crypto, cripto, bitcoin, ethereum, criptomoneda, criptomonedas, trading, "
        "inversion, inversiones, investment, investing, forex, impuesto, impuestos, tax, taxes, dian, "
        "declaracion de renta, nomina, salario, salary, sueldo, pension, pensiones, cesantias, colpensiones, "
        "factura, facturas, invoice, cobro, cobros, pagar, metamask, wallet, billetera, "
        # crédito, vivienda y fintech
        "hipoteca, hipotecas, hipotecario, leasing, addi, sistecredito, tyba, cdt, cdts, fiducia, fiduciaria, "
        "rappipay, movii, efecty, datacredito, centrales de riesgo, puntaje crediticio, refinanciar, "
        "refinanciacion, cobranza, remesa, remesas, western union, "
        # apuestas
        "apuesta, apuestas, casino, casinos, betplay, wplay, rushbet, codere, yajuego, bet365"),
    "legal y trámites": _terminos(
        "abogado, abogada, abogados, lawyer, demanda laboral, demanda judicial, demanda civil, "
        "demanda de divorcio, demanda por alimentos, poner una demanda, demandar, demandado, demandante, "
        "lawsuit, juzgado, juez, judge, tribunal, court, fiscalia, procuraduria, contraloria, rama judicial, "
        "proceso judicial, tutela, derecho de peticion, notaria, notario, divorcio, divorce, custodia, "
        "herencia familiar, derecho de herencia, proceso de sucesion, juicio de sucesion, testamento, denuncia, "
        "denuncias, policia, police, multa, multas, comparendo, simit, antecedentes, carcel, prision, prison, "
        "pasaporte, passport, cedula, visa, migracion, embargo, desalojo, desalojar, restitucion, "
        "restitucion de inmueble, querella, conciliacion, juicio, orden de captura, medida cautelar, "
        "comisaria de familia, violencia intrafamiliar, inspeccion de policia, apostilla, personeria, "
        "defensoria"),
}

# Raíces de categorías especiales: un token que empieza por una de ellas es sensible
# («psiquiatría», «ecografía», «hipotecario», «desalojaron»…).
_PREFIJOS_SENSIBLES: Final[Mapping[str, tuple[str, ...]]] = {
    "salud": ("psiquiatr", "psicoterap", "antidepres", "ansiolit", "ecograf", "radiograf", "tomograf",
              "mamograf", "oncolog", "quimioterap", "radioterap", "neuropsicolog", "hipotiroid", "hipertens",
              "anticoncep", "menstru", "ginecolog", "urolog", "odontolog", "fisioterap", "infertil", "embaraz",
              "farmaceut", "drogueri", "psicolog", "metilfenid", "trastorn"),
    "sexualidad": ("pornogr", "masturb", "transexu", "homosexu", "bisexu", "erotic", "erotis"),
    "política": ("uribis", "petris", "chavis", "comunis", "socialis", "fascis"),
    "finanzas personales": ("hipotec", "refinanci", "criptomoned", "prestamist"),
    "legal y trámites": ("desaloj", "abogad", "notari"),
}


def _mapas_lexico(lexico: Mapping[str, tuple[str, ...]]) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """``(palabra → categoría, ((frase, categoría), …))`` de un léxico."""
    palabras: dict[str, str] = {}
    frases: list[tuple[str, str]] = []
    for categoria, terminos in lexico.items():
        for termino in terminos:
            if " " in termino:
                frases.append((f" {termino} ", categoria))
            else:
                palabras.setdefault(termino, categoria)
    return palabras, tuple(frases)


_PALABRAS_INTERES, _FRASES_INTERES = _mapas_lexico(_INTERESES)
_PALABRAS_SENSIBLES, _FRASES_SENSIBLES = _mapas_lexico(_SENSIBLES)
# Raíces agrupadas por sus 4 primeras letras (búsqueda barata en 27 000 títulos).
_RAICES_SENSIBLES: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    clave: tuple(par for par in ((p, c) for c, ps in _PREFIJOS_SENSIBLES.items() for p in ps) if par[0][:4] == clave)
    for clave in {p[:4] for ps in _PREFIJOS_SENSIBLES.values() for p in ps}
}


def _categoria_sensible_token(token: str) -> str | None:
    """Categoría especial de un token normalizado (palabra del léxico o raíz), o ``None``."""
    categoria = _PALABRAS_SENSIBLES.get(token)
    if categoria is not None:
        return categoria
    for raiz, cat in _RAICES_SENSIBLES.get(token[:4], ()):
        if token.startswith(raiz):
            return cat
    return None

# Dominio registrable → categoría de interés (visitas de Chrome).
_DOMINIOS_INTERES: Final[Mapping[str, str]] = {
    dominio: categoria
    for categoria, dominios in {
        "programación y desarrollo": (
            "github.com", "gitlab.com", "stackoverflow.com", "stackexchange.com", "npmjs.com", "pypi.org",
            "python.org", "w3schools.com", "freecodecamp.org", "dev.to", "vercel.com", "netlify.com",
            "docker.com", "nextjs.org", "react.dev", "reactjs.org", "nodejs.org", "typescriptlang.org",
            "rust-lang.org", "go.dev", "prisma.io", "tailwindcss.com", "vitejs.dev", "digitalocean.com",
            "hostinger.com", "cloudpanel.io", "cloudflare.com", "codepen.io", "replit.com", "leetcode.com",
            "hackerrank.com", "postman.com", "readthedocs.io", "readthedocs.org", "github.io", "mozilla.org",
            "jsdelivr.com", "electronjs.org", "sqlite.org", "postgresql.org", "godaddy.com", "namecheap.com"),
        "inteligencia artificial": (
            "chatgpt.com", "openai.com", "claude.ai", "anthropic.com", "perplexity.ai", "huggingface.co",
            "poe.com", "character.ai", "deepseek.com", "mistral.ai", "midjourney.com", "civitai.com",
            "ollama.com", "lmarena.ai", "puter.com", "elevenlabs.io", "runwayml.com", "leonardo.ai", "suno.com",
            "udio.com", "groq.com", "together.ai", "openrouter.ai", "cursor.com", "replicate.com", "kaggle.com"),
        "anime, manga y VTubers": (
            "crunchyroll.com", "myanimelist.net", "animeflv.net", "anilist.co", "mangadex.org", "vroid.com",
            "booth.pm", "hololive.tv", "live2d.com"),
        "videojuegos": (
            "steampowered.com", "steamcommunity.com", "epicgames.com", "twitch.tv", "playstation.com", "xbox.com",
            "nintendo.com", "riotgames.com", "minecraft.net", "roblox.com", "itch.io", "ign.com", "gamespot.com",
            "nexusmods.com", "curseforge.com", "speedrun.com", "howlongtobeat.com", "gog.com", "humblebundle.com",
            "ea.com", "ubisoft.com", "blizzard.com", "battle.net", "hoyoverse.com"),
        "música": (
            "spotify.com", "soundcloud.com", "deezer.com", "last.fm", "genius.com", "bandcamp.com",
            "ultimate-guitar.com", "lacuerda.net", "musescore.com", "letras.com"),
        "cine y series": (
            "netflix.com", "disneyplus.com", "primevideo.com", "hbomax.com", "max.com", "imdb.com",
            "filmaffinity.com", "letterboxd.com", "justwatch.com", "paramountplus.com", "mubi.com",
            "rottentomatoes.com"),
        "video y streaming": ("youtube.com", "youtu.be", "vimeo.com", "kick.com", "obsproject.com", "capcut.com"),
        "ciencia": (
            "arxiv.org", "nature.com", "sciencedirect.com", "nasa.gov", "researchgate.net", "sciencemag.org",
            "newscientist.com", "quantamagazine.org", "wolframalpha.com", "flywire.ai"),
        "tecnología y hardware": (
            "xataka.com", "theverge.com", "techcrunch.com", "arstechnica.com", "tomshardware.com", "gsmarena.com",
            "wired.com", "engadget.com", "hipertextual.com", "genbeta.com", "microsoft.com", "apple.com",
            "samsung.com", "xiaomi.com", "nvidia.com", "amd.com", "intel.com", "arduino.cc", "raspberrypi.com"),
        "diseño y creatividad": (
            "figma.com", "canva.com", "behance.net", "dribbble.com", "adobe.com", "blender.org", "sketchfab.com",
            "artstation.com", "deviantart.com", "coolors.co", "unsplash.com", "freepik.com", "pexels.com",
            "flaticon.com", "pinterest.com"),
        "negocios y emprendimiento": (
            "shopify.com", "hubspot.com", "mailchimp.com", "semrush.com", "ahrefs.com", "wix.com", "upwork.com",
            "fiverr.com"),
        "inmobiliario y arriendos": ("metrocuadrado.com", "fincaraiz.com.co", "ciencuadras.com", "alquile.com.co"),
        "educación e idiomas": (
            "udemy.com", "coursera.org", "platzi.com", "edx.org", "khanacademy.org", "duolingo.com",
            "domestika.org", "crehana.com", "rae.es", "wordreference.com", "deepl.com"),
        "noticias y actualidad": (
            "eltiempo.com", "elespectador.com", "semana.com", "bbc.com", "cnn.com", "elpais.com", "infobae.com",
            "nytimes.com", "reuters.com", "pulzo.com", "elcolombiano.com", "larepublica.co"),
        "deportes": (
            "espn.com", "marca.com", "as.com", "fifa.com", "uefa.com", "nba.com", "futbolred.com",
            "winsports.co", "onefootball.com", "transfermarkt.com", "flashscore.com", "sofascore.com",
            "chess.com", "lichess.org"),
        "cocina y comida": (
            "allrecipes.com", "cookpad.com", "recetasgratis.net", "rappi.com", "rappi.com.co", "directoalpaladar.com",
            "kiwilimon.com"),
        "viajes": (
            "booking.com", "airbnb.com", "despegar.com", "avianca.com", "latamairlines.com", "tripadvisor.com",
            "expedia.com", "kayak.com", "skyscanner.com", "wingo.com", "trivago.com"),
        "compras en línea": (
            "mercadolibre.com", "mercadolibre.com.co", "amazon.com", "aliexpress.com", "temu.com", "shein.com",
            "ebay.com", "falabella.com", "falabella.com.co", "exito.com", "alkosto.com", "ktronix.com",
            "dafiti.com.co", "homecenter.com.co", "olx.com"),
        "redes sociales": (
            "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "reddit.com", "discord.com",
            "linkedin.com", "threads.net", "tumblr.com", "bsky.app", "whatsapp.com", "telegram.org"),
        "productividad y ofimática": (
            "notion.so", "trello.com", "asana.com", "todoist.com", "monday.com", "clickup.com", "miro.com",
            "office.com", "dropbox.com", "evernote.com", "obsidian.md", "zapier.com", "n8n.io", "make.com",
            "calendly.com", "ilovepdf.com", "smallpdf.com"),
    }.items()
    for dominio in dominios
}

# Dominios de categorías especiales, sobre la primera etiqueta del dominio registrable (sin guiones;
# ``cruz-verde.com.co`` → ``cruzverde``). Por categoría: (subcadenas inequívocas que pueden ir en
# cualquier parte, etiquetas exactas o partes entre guiones, prefijos de etiqueta). Las cadenas
# cortas o ambiguas nunca van como subcadena («sex» en ``essex``/``lasexta``, «dental» en
# ``occidental``, «partido» en ``partidosdelaliga``, «petro» en ``petrobras``).
_SENSIBLE_DOMINIO: Final[Mapping[str, tuple[tuple[str, ...], frozenset[str], tuple[str, ...]]]] = {
    "finanzas personales": (
        ("banco", "bank", "bancolombia", "davivienda", "daviplata", "nequi", "colpatria", "scotiabank", "avvillas",
         "bancoomeva", "bancamia", "paypal", "payoneer", "binance", "coinbase", "bitso", "metamask",
         "westernunion", "colpensiones", "skandia", "datacredito", "transunion", "experian", "crypto", "cripto",
         "bitcoin", "forex", "trading", "prestamo", "credito", "inversion", "investing", "tarjeta", "pagos",
         "facturacion", "siigo", "stripe", "wompi", "epayco", "payu", "mercadopago", "remesa", "bbva", "itau",
         "sistecredito", "rappipay", "efecty", "fiduciaria", "hipotec", "leasing", "betplay", "wplay", "rushbet",
         "codere", "yajuego", "bet365", "betfair", "casino", "apuesta"),
        frozenset({"addi", "tyba", "movii", "nu", "porvenir", "alegra", "kraken", "stake", "dian", "pse", "lulo"}),
        ()),
    "salud": (
        ("salud", "health", "clinic", "hospital", "farmac", "pharma", "drogueria", "doctoralia", "sanitas",
         "famisanar", "coomeva", "colmedica", "mayoclinic", "webmd", "medlineplus", "psicolog", "psiquiatr",
         "therapy", "terapia", "nutricion", "odontolog", "dentist", "laboratorio", "colsubsidio", "epssura",
         "nuevaeps", "sisben", "minsalud", "cruzverde", "farmatodo", "larebaja", "locatel", "profamilia",
         "oncolog", "medicin"),
        frozenset({"cafam", "compensar", "sura", "segurossura", "eps", "ips", "coosalud", "medimas"}),
        ("dental", "medic")),
    "sexualidad": (
        ("porn", "xxx", "hentai", "xvideos", "xnxx", "xhamster", "onlyfans", "chaturbate", "redtube", "youporn",
         "brazzers", "erotic", "nsfw", "rule34", "tinder", "grindr", "bumble", "badoo", "fansly", "stripchat",
         "spankbang", "eporner", "camsoda", "fapello", "danbooru", "gelbooru", "e621", "lgbt", "erome", "missav",
         "f95zone", "nhentai", "escort", "prepago", "sexo"),
        frozenset({"jav", "javhd"}),
        ("sex",)),
    "religión": (
        ("iglesia", "church", "biblia", "bible", "catholic", "catolic", "vatican", "evangel", "islam", "quran",
         "coran", "torah", "budis", "buddh", "mormon", "adventist", "pentecost", "parroquia", "diocesis"),
        frozenset({"jw"}),
        ("cristian", "christian")),
    "política": (
        ("pactohistorico", "centrodemocratico", "colombiahumana", "partidoliberal", "partidoconservador",
         "cambioradical", "polodemocratico", "alianzaverde", "registraduria", "elecciones", "election",
         "presidencia", "whitehouse"),
        frozenset({"petro", "gustavopetro", "uribe", "alvarouribe", "senado", "senate", "congreso", "congress"}),
        ("politic",)),
    "legal y trámites": (
        ("ramajudicial", "fiscalia", "procuraduria", "contraloria", "policia", "abogad", "lawyer", "juzgado",
         "notaria", "tutela", "migracion", "cancilleria", "simit", "supernotariado"),
        frozenset({"runt"}),
        ()),
}
# Dominios del Estado (trámites): se tratan como "legal y trámites", salvo divulgación científica.
_SUFIJOS_ESTADO: Final = (".gov", ".mil", ".gob")
_ESTADO_PERMITIDOS: Final = frozenset({"nasa.gov", "noaa.gov", "nist.gov", "usgs.gov"})

# Sufijos de dos niveles (dominio registrable = tres etiquetas).
_SUFIJOS_DOBLES: Final = frozenset("""
com.co net.co org.co edu.co gov.co mil.co nom.co co.uk org.uk ac.uk gov.uk me.uk com.mx org.mx gob.mx
edu.mx net.mx com.ar gob.ar org.ar net.ar com.br gov.br org.br net.br com.pe gob.pe org.pe edu.pe com.ec
gob.ec com.ve gob.ve gob.cl com.uy gub.uy com.py gov.py com.bo gob.bo com.gt gob.gt com.pa gob.pa co.cr go.cr
com.do gob.do com.sv gob.sv com.hn gob.hn com.ni gob.ni com.cu com.pr gob.es com.es org.es com.au net.au
org.au gov.au edu.au co.nz co.jp ne.jp or.jp co.kr co.in gov.in co.za gov.za com.cn gov.cn com.hk com.sg
com.tr gov.tr com.tw co.il mil.ar
""".split())
# Sufijos "privados" donde el subdominio es de un usuario o cliente (p. ej. ``alguien.github.io``):
# se colapsan a la plataforma para no guardar nombres de usuario ajenos.
_SUFIJOS_PLATAFORMA: Final = (
    "github.io", "gitlab.io", "vercel.app", "netlify.app", "herokuapp.com", "blogspot.com", "web.app",
    "firebaseapp.com", "pages.dev", "workers.dev", "onrender.com", "appspot.com", "glitch.me", "repl.co",
    "replit.app", "replit.dev", "wordpress.com", "wixsite.com", "carrd.co", "notion.site", "substack.com",
    "medium.com", "tumblr.com", "itch.io", "ngrok.io", "ngrok-free.app", "azurewebsites.net",
    "cloudfront.net", "amazonaws.com", "translate.goog", "duckdns.org", "sharepoint.com", "my.salesforce.com",
    "fly.dev", "railway.app", "streamlit.app", "hf.space", "gradio.live", "trycloudflare.com",
    "atlassian.net", "myshopify.com", "webflow.io", "framer.website",
)
_SUFIJOS_NO_PUBLICOS: Final = (".local", ".localhost", ".internal", ".lan", ".home", ".test", ".example",
                               ".invalid", ".arpa", ".localdomain", ".corp")
_RE_DOMINIO_VALIDO: Final = re.compile(r"^(?=.{4,100}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_TLD_GENERICOS: Final = frozenset("com org net edu gov mil int info biz app dev io co me tv ai xyz gg ly gl be "
                                  "es mx ar cl pe ec ve br uk us de fr it jp kr in cn ru".split())

# Sitios que sí se pueden **nombrar** en el perfil (plataformas, sitios guardados y servicios que
# escriben): los del léxico de intereses y estos, conocidos y no sensibles. Cualquier otro dominio
# solo se cuenta (``sitios_sin_nombrar``): un dominio desconocido puede ser el de una persona
# (``nombreapellido.com``), un cliente o una categoría especial que el léxico no conoce.
_DOMINIOS_NEUTROS: Final = frozenset("""
google.com wikipedia.org wikimedia.org wiktionary.org wikihow.com archive.org quora.com medium.com substack.com
blogspot.com wordpress.com translate.goog microsoft.com live.com outlook.com bing.com icloud.com zoom.us
slack.com atlassian.com atlassian.net dropbox.com wetransfer.com indeed.com render.com railway.app heroku.com
supabase.com firebase.com jetbrains.com visualstudio.com sentry.io twilio.com sendgrid.com mailgun.com resend.com
contabo.com hetzner.com ovhcloud.com linode.com vultr.com letsencrypt.org ngrok.com expo.dev unity.com
unrealengine.com godotengine.org grok.com uber.com didiglobal.com claro.com.co movistar.co tigo.com.co
""".split())
_DOMINIOS_CONOCIDOS: Final = frozenset(_DOMINIOS_INTERES) | _DOMINIOS_NEUTROS
# Dominios de infraestructura (anuncios, redirecciones, CDNs e inicio de sesión): no son "sitios que
# usa"; sus visitas solo cuentan para el ritmo. ``google.com`` va aparte, en ``servicios_google``.
_DOMINIOS_INFRAESTRUCTURA: Final = frozenset("""
googleadservices.com doubleclick.net googlesyndication.com googletagmanager.com google-analytics.com
googleusercontent.com gstatic.com googleapis.com ggpht.com ytimg.com t.co bit.ly lnkd.in tinyurl.com ow.ly
buff.ly shorturl.at cutt.ly rebrand.ly is.gd goo.gl microsoftonline.com msftauth.net akamaihd.net cloudfront.net
amazonaws.com fbcdn.net cdninstagram.com twimg.com doubleverify.com
""".split())
_PREFIJOS_HOST_INFRA: Final = ("accounts.", "consent.", "login.", "auth.", "sso.", "signin.", "oauth.",
                               "myaccount.", "account.")
# Mapas y movilidad: sus títulos son lugares (direcciones, conjuntos, veredas): nunca se leen.
_DOMINIOS_MAPAS: Final = frozenset({
    "waze.com", "uber.com", "didiglobal.com", "cabify.com", "indrive.com", "moovitapp.com", "openstreetmap.org",
    "here.com", "mapquest.com", "citymapper.com", "goo.gl", "maps.me",
})
# Webmails (Roundcube, Horde, Zimbra… en dominio propio): sus títulos son asuntos de correos.
_PREFIJOS_WEBMAIL: Final = frozenset({"webmail", "mail", "correo", "email", "owa", "roundcube", "zimbra", "horde",
                                      "squirrelmail", "rainloop", "snappymail", "imap"})
_CLAVES_WEBMAIL: Final = frozenset({"_task", "_mbox", "_action", "_uid"})
_RE_TITULO_WEBMAIL: Final = re.compile(
    r"(?i)\b(?:roundcube|horde|zimbra|squirrelmail|rainloop|snappymail|outlook web app|webmail|"
    r"bandeja de entrada|inbox|correo web)\b")

# Títulos que nunca se leen (correo, mensajería, perfiles sociales, cuentas, calendario, notas,
# documentos y archivos en la nube, mapas…): pueden traer asuntos, nombres o lugares de terceros.
_DOMINIOS_TITULO_IGNORADO: Final = frozenset({
    "whatsapp.com", "telegram.org", "messenger.com", "facebook.com", "instagram.com", "linkedin.com", "x.com",
    "twitter.com", "tiktok.com", "discord.com", "slack.com", "outlook.com", "live.com", "office.com",
    "office365.com", "microsoft365.com", "yahoo.com", "zoom.us", "proton.me", "protonmail.com", "icloud.com",
    "zoho.com", "hotmail.com", "skype.com", "snapchat.com", "threads.net", "bsky.app", "signal.org",
    "dropbox.com", "wetransfer.com", "mega.nz", "box.com", "1drv.ms", "scribd.com", "sharepoint.com",
}) | _DOMINIOS_MAPAS
_HOSTS_TITULO_IGNORADO: Final = frozenset({
    "mail.google.com", "inbox.google.com", "contacts.google.com", "calendar.google.com", "photos.google.com",
    "myaccount.google.com", "accounts.google.com", "meet.google.com", "chat.google.com", "voice.google.com",
    "messages.google.com", "keep.google.com", "takeout.google.com", "teams.microsoft.com", "outlook.office.com",
    "outlook.live.com", "web.whatsapp.com", "web.telegram.org",
    # Documentos y archivos (nombres de archivos propios o compartidos por otros): Drive ya se lee
    # desde el índice del zip.
    "docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com", "forms.google.com",
    "script.google.com", "colab.research.google.com", "lookerstudio.google.com", "jamboard.google.com",
    # Mapas.
    "maps.google.com", "earth.google.com", "maps.app.goo.gl",
})
# Títulos que son consultas o conversaciones del propio usuario (buscadores y chats de IA) o que
# llevan ``dueño/repo`` de otras personas (forjas de código): solo cuentan para las categorías del
# léxico, nunca como palabras clave.
_DOMINIOS_SOLO_CATEGORIAS: Final = frozenset({
    "chatgpt.com", "openai.com", "claude.ai", "perplexity.ai", "poe.com", "character.ai", "deepseek.com",
    "bing.com", "duckduckgo.com", "ecosia.org", "yandex.com", "baidu.com", "startpage.com", "you.com",
    "copilot.microsoft.com", "phind.com", "lmarena.ai", "mistral.ai", "grok.com",
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "huggingface.co",
})
_HOSTS_SOLO_CATEGORIAS: Final = frozenset({
    "gemini.google.com", "bard.google.com", "aistudio.google.com", "notebooklm.google.com", "search.brave.com",
    "copilot.microsoft.com", "chat.openai.com", "chat.mistral.ai", "chat.deepseek.com",
})
_CLAVES_BUSQUEDA: Final = frozenset({"q", "query", "search_query", "search", "buscar", "k", "keyword", "keywords",
                                     "text", "p", "term", "searchterm", "busqueda", "oq"})
_RE_RUTA_BUSQUEDA: Final = re.compile(r"/(?:search|results|buscar|busqueda|s)/?$", re.I)

# Servicios de Google por host (google.com es un solo dominio registrable).
_SERVICIOS_GOOGLE: Final[Mapping[str, str]] = {
    "mail.google.com": "Gmail", "docs.google.com": "Google Docs", "drive.google.com": "Google Drive",
    "calendar.google.com": "Google Calendar", "meet.google.com": "Google Meet", "gemini.google.com": "Gemini",
    "bard.google.com": "Gemini", "photos.google.com": "Google Fotos", "maps.google.com": "Google Maps",
    "translate.google.com": "Google Translate", "keep.google.com": "Google Keep",
    "classroom.google.com": "Google Classroom", "aistudio.google.com": "Google AI Studio",
    "notebooklm.google.com": "NotebookLM", "console.cloud.google.com": "Google Cloud",
    "cloud.google.com": "Google Cloud", "colab.research.google.com": "Colab",
    "scholar.google.com": "Google Académico", "news.google.com": "Google Noticias", "play.google.com": "Google Play",
    "sites.google.com": "Google Sites", "fonts.google.com": "Google Fonts",
    "analytics.google.com": "Google Analytics", "search.google.com": "Search Console",
    "chat.google.com": "Google Chat", "lens.google.com": "Google Lens",
}

# Palabras que nunca son "palabras clave" (además de STOPWORDS_ES): inglés funcional, genéricas
# de la web, del Takeout, meses y días.
_STOPWORDS_EN: Final = frozenset("""
about above after again against also among another because been before being below between both cannot could
does doing done down during each from further having here hers herself himself into itself just like make made
more most much must myself once only other ours over same should some such than that their theirs them then
there these they this those through under until upon very were what when where which while whom will with
within without would your yours yourself using used gets with what your have will this from that they them
then than into onto over some best free tips ways guide part full official
""".split())
_GENERICAS: Final = frozenset(_sin_tildes(p) for p in """
inicio home login logout iniciar sesion session signin signup registro register cuenta cuentas account accounts
pagina paginas page pages pestana pestanas untitled titulo titled error errores cargando loading bienvenido
bienvenida welcome oficial official sitio site sites website online gratis descargar download downloads
video videos imagen imagenes image images nuevo nueva nuevos nuevas mejor mejores todos todas inbox recibidos
bandeja entrada notificaciones notifications search buscar busqueda resultados results google chrome youtube
perfil profile settings configuracion ajustes ayuda help soporte support contacto contact politica privacidad
privacy terminos terms dashboard panel principal main default index general archivo archivos file files carpeta
carpetas folder folders documento documentos document documents compartido compartidos shared copia copy
version final editar edit vista view preview abrir open cerrar close guardar save subir upload enlace link links
mensaje mensajes message messages chat chats correo correos email emails mail bookmarks bookmark marcadores
favoritos barra other otros mobile movil lista reading lectura whatsapp screenshot captura pantalla grabacion
recording photo foto fotos picture camera scan escaneo escaneado copia nuevo-documento sintitulo
lunes martes miercoles jueves viernes sabado domingo monday tuesday wednesday thursday friday saturday sunday
enero febrero marzo abril mayo junio julio agosto septiembre setiembre octubre noviembre diciembre january
february march april june july august september october november december hora horas minutos segundos semana
semanas meses year years parte partes capitulo chapter numero number total precio price comprar
""".split())

_NOMBRES_EN: Final = frozenset("""
james michael robert william mary patricia jennifer linda elizabeth susan jessica sarah karen thomas charles
christopher matthew anthony mark donald steven paul andrew joshua kenneth kevin brian george timothy ronald
edward jason jeffrey ryan jacob gary nicholas eric jonathan stephen larry justin scott brandon benjamin samuel
gregory alexander frank patrick raymond jack dennis jerry tyler aaron adam nathan henry douglas zachary peter
kyle noah ethan jeremy walter keith roger terry austin sean gerald carl harold dylan arthur lawrence jordan
jesse bryan billy bruce logan alan albert willie elijah wayne randy vincent mason ralph bobby russell bradley
philip eugene nancy betty margaret sandra ashley kimberly emily donna michelle carol amanda melissa deborah
stephanie dorothy rebecca sharon cynthia kathleen angela shirley brenda emma anna pamela nicole samantha
katherine christine helen debra rachel carolyn janet catherine heather diane olivia julie joyce victoria ruth
virginia lauren kelly christina joan evelyn judith hannah megan cheryl jacqueline martha madison teresa gloria
janice kathryn abigail sophia frances jean alice judy isabella julia grace amber denise danielle marilyn
beverly charlotte natalie theresa diana brittany doris kayla alexis lori marie
""".split())
_APELLIDOS_COMUNES: Final = frozenset("""
garcia rodriguez martinez lopez gonzalez perez sanchez ramirez torres flores rivera gomez diaz cruz morales
reyes gutierrez ortiz ramos castillo jimenez moreno romero herrera medina aguilar vargas castro rojas suarez
munoz mendoza ruiz alvarez cardenas restrepo zuluaga osorio giraldo mejia ospina arango cardona quintero
valencia rincon salazar montoya henao cano duque marin velez hernandez guzman pineda bedoya correa londono
agudelo ocampo villa franco cortes acosta benitez beltran caicedo camacho cifuentes contreras escobar
espinosa fajardo galeano gallego guerrero gil hoyos jaramillo leon lozano mora murillo navarro nunez orozco
parra patino pena pulido quintana rubio sierra silva soto toro trujillo uribe vasquez vega zapata smith
johnson williams brown jones miller davis wilson anderson taylor moore jackson martin thompson white harris
""".split())
_NOMBRES_PROPIOS_COMUNES: Final = NOMBRES_COMUNES_ES | _NOMBRES_EN | _APELLIDOS_COMUNES

# Lugares genéricos (direcciones, conjuntos, veredas…): nunca son palabra clave.
_TOPONIMOS: Final = frozenset("""
vereda veredas barrio barrios conjunto conjuntos torre torres apto aptos calle calles carrera carreras avenida
avenidas finca fincas urbanizacion edificio edificios manzana manzanas lote lotes municipio corregimiento
localidad comuna sector piso local casa casas ciudad pueblo direccion ubicacion residencial
""".split())
# Términos del léxico demasiado genéricos para ser palabra clave (sí cuentan para las categorías).
_NO_CLAVE: Final = frozenset("""
aprender learn learning comprar compras oferta ofertas descuento descuentos noticia noticias news articulo
articulos guia guide ia ai ui ux
""".split())
# Temas neutros fuera de las categorías que sí dicen algo de la persona (vocabulario cerrado).
_VOCAB_TEMAS: Final = frozenset("""
proyecto proyectos tesis informe informes reporte reportes manual manuales ensayo ensayos apuntes libro libros
novela novelas cuento cuentos comic comics poesia historia filosofia robot robots robotica dron drones maqueta
maquetas origami lego rompecabezas aplicacion aplicaciones app apps dominio dominios plugin plugins bot bots
producto productos catalogo marca marcas cliente plantilla presentacion presentaciones portafolio portfolio
""".split())
_FRASES_TEMAS: Final = ("pagina web", "sitio web", "juegos de mesa")
# Palabras que pueden formar el nombre de una extensión o de un Gem (además del vocabulario):
# roles y piezas de herramientas, nunca nombres de personas.
_PALABRAS_HERRAMIENTA: Final = frozenset("""
tutor tutora asistente ayudante experto experta coach mentor profesor profesora maestro guia revisor redactor
editor corrector traductor traduccion generador creador planificador organizador analista analizador consultor
asesor entrenador escritor narrador resumidor explicador disenador ilustrador chef cocinero critico doctor
lector atajos atajo notas nota tareas tarea recordatorio recordatorios bloqueador blocker adblock ads block dark
mode modo oscuro reader viewer visor formatter formateador tools tool devtools developer developers herramienta
herramientas password passwords manager gestor contrasenas color colors picker selector translate translator
spell grammar ortografia gramatica clipper page tab tabs session speed controller control downloader cookie
cookies vpn proxy extension plus pro lite mini light assistant helper builder writer reviewer planner generator
creator summarizer explainer for the and of to in on my with by an it
""".split())
# Variantes que se cuentan como un solo término (además de singular/plural).
_ALIAS_CLAVE: Final[Mapping[str, str]] = {
    "reactjs": "react", "vuejs": "vue", "three js": "threejs", "node js": "nodejs", "next js": "nextjs",
    "java script": "javascript", "stack overflow": "stackoverflow", "hugging face": "huggingface",
    "e commerce": "ecommerce", "mercado libre": "mercadolibre", "finca raiz": "fincaraiz", "raspberry": "raspberry pi",
    "steins": "steins gate", "postgresql": "postgres", "davinci": "davinci resolve", "llms": "llm",
}


def _construir_vocabulario() -> tuple[frozenset[str], dict[tuple[str, ...], str], dict[str, str]]:
    """``(palabras, frases → término, término → canónico)`` del vocabulario cerrado de palabras clave.

    Solo términos del léxico de intereses y de ``_VOCAB_TEMAS``: así una palabra clave nunca es un
    nombre, un apodo, un *handle* ni un lugar sacado de un título. Los plurales se cuentan con su
    singular («juegos» → «juego») y los alias con su forma canónica («reactjs» → «react»).
    """
    palabras = set(_PALABRAS_INTERES) | set(_VOCAB_TEMAS)
    frases: dict[tuple[str, ...], str] = {}
    for terminos in _INTERESES.values():
        for termino in terminos:
            if " " in termino:
                frases[tuple(termino.split())] = termino
    for termino in _FRASES_TEMAS:
        frases[tuple(termino.split())] = termino
    canonica: dict[str, str] = {}
    for palabra in palabras:
        if palabra.endswith("s") and palabra[:-1] in palabras:
            canonica[palabra] = palabra[:-1]
        elif palabra.endswith("es") and palabra[:-2] in palabras:
            canonica[palabra] = palabra[:-2]
    canonica.update(_ALIAS_CLAVE)
    return frozenset(palabras), frases, canonica


_VOCAB_PALABRAS, _VOCAB_FRASES, _CANONICA = _construir_vocabulario()
_MAX_FRASE: Final = max(len(f) for f in _VOCAB_FRASES)

_RE_PALABRA_LETRAS: Final = re.compile(r"[^\W\d_]+")
_RE_TOKEN_SUPERFICIE: Final = re.compile(r"[^\W_]+")
_RE_TOKEN_LEXICO: Final = re.compile(r"[a-z0-9]+")
_RE_SOLO_LATINO: Final = re.compile(r"^[a-z]+$")
_RE_HASH: Final = re.compile(r"^(?:[0-9a-f]{16,}|[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})$")
_RE_CAMEL: Final = re.compile(r"(?<=[a-záéíóúñ])(?=[A-ZÁÉÍÓÚÑ])")
_RE_SEPARADORES_NOMBRE: Final = re.compile(r"[_.\-+~]+")
_RE_COPIA_NUMERADA: Final = re.compile(r"\s*\(\d+\)\s*$")
_RE_PREFIJO_COPIA: Final = re.compile(r"^(?:copia de|copy of)\s+", re.I)
_RE_URL_O_CORREO: Final = re.compile(r"(?i)(?:://|\bwww\.|@)")
_RE_DIGITOS_LARGOS: Final = re.compile(r"\d{7,}")
# Separadores entre dígitos de teléfonos y cédulas (``300-123-4567``, ``1.036.123.456``, ``(300) 123 4567``).
_RE_SEPARADOR_DIGITOS: Final = re.compile(r"(?<=\d)[\s.\-()/]+(?=\d)")
_RE_CONTADOR_TITULO: Final = re.compile(r"^\s*\(\d+\+?\)\s*")          # «(3) canal - YouTube»
_RE_DUENO_REPO: Final = re.compile(r"\S*/\S*")                          # «dueño/repo», rutas
_RE_SEPARADOR_SITIO: Final = re.compile(r"\s+(?:[-|·—–]|::)\s+")
_RE_DIRECCION: Final = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_RE_DIRECCION_ANGULOS: Final = re.compile(r"<\s*([\w.+'-]+@[\w-]+(?:\.[\w-]+)+)\s*>")
_RE_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_RE_ESPACIOS: Final = re.compile(r"\s+")

# Extensiones de Chrome por id (el Takeout casi nunca trae ``name``). Solo productos conocidos.
_EXTENSIONES_CONOCIDAS: Final[Mapping[str, str]] = {
    "cjpalhdlnbpafiamejdnhcphjbkeiagm": "uBlock Origin",
    "ddkjiahejlhfcafbddmgiahcphecmpfh": "uBlock Origin Lite",
    "gighmmpiobklfepjocnamgkkbiglidom": "AdBlock",
    "cfhdojbkjhnklbpkdaibdccddilifddb": "Adblock Plus",
    "bgnkhhnnamicmpeenaelnjfhikgbkllg": "AdGuard",
    "aapbdbdomjkkjkaonfhkkikfgjllcleb": "Google Translate",
    "ghbmnnjooekpmoecnnnilnnbdlolhkhi": "Documentos de Google sin conexión",
    "fmkadmapgofadopljbjfkapdkoienihi": "React Developer Tools",
    "nhdogjmejiglipccpnnnanhbledajbpd": "Vue.js devtools",
    "lmhkpmbekcpmknklioeibfkpmmfibljd": "Redux DevTools",
    "kbfnbcaeplbcioakkpcpgfkobkghlhen": "Grammarly",
    "nngceckbapebfimnlniiiahkandclblb": "Bitwarden",
    "hdokiejnpimakedhajhdlcegeplioahd": "LastPass",
    "aeblfdkhhhdcdjpifhhbdiojplfjncoa": "1Password",
    "nkbihfbeogaeaoehlefnkodbefgpgknn": "MetaMask",
    "bfnaelmomeimhlpmgjnjophhpkkoljpa": "Phantom",
    "eimadpbcbfnmbkopoojfekhnkhdbieeh": "Dark Reader",
    "dhdgffkkebhmkfjojejmpbldmpobfkfo": "Tampermonkey",
    "bmnlcjabgnpnenekpadlanbbkooimhnj": "Honey",
    "gppongmhjkpfnbhagpmjfkannfbllamg": "Wappalyzer",
    "bcjindcccaagfpapjjmafapmmgkkhgoa": "JSON Formatter",
    "efaidnbmnnnibpcajpcglclefindmkaj": "Adobe Acrobat",
    "mnjggcdmjocbbbhaepdhchncahnbgone": "SponsorBlock",
    "gebbhagfogifgggkldgodflihgfeippi": "Return YouTube Dislike",
    "pkehgijcmpdhfbdbbnkijodmdjhbjlgp": "Privacy Badger",
    "ldpochfccmkkmhdbclfhpagapcfdljkj": "Decentraleyes",
    "hkgfoiooedgoejojocmhlaklaeopbecg": "Picture-in-Picture",
    "ponfpcnoihfmfllpaingbgckeeldkhle": "Enhancer for YouTube",
    "chphlpgkkbolifaimnlloiipkdnihall": "OneTab",
    "fdpohaocaechififmbbbbbknoalclacl": "GoFullPage",
    "bhlhnicpbhignbdhedgjhgdocnmhomnp": "ColorZilla",
    "liecbddmkiiihnedobmlmillhodjkdmb": "Loom",
    "hlkenndednhfkekhgcdicdfddnkalmdm": "Cookie-Editor",
    "oocalimimngaihdkbihfgmpkcpnmlaoa": "Teleparty",
    "fjoaledfpmneenckfbpdfhkmimnjocfa": "NordVPN",
    "lpcaedmchfhocbbapmcbpinfpgnhiddi": "Google Keep",
    "jlmpjdjjbgclbocgajdjefcidcncaied": "daily.dev",
    "gcbommkclmclpchllfjekcdonpmejbdp": "HTTPS Everywhere",
}
# Componentes internos de Chrome (no son herramientas del usuario).
_EXTENSIONES_INTERNAS: Final = frozenset({
    "ahfgeienlihckogmohjhadlkjgocpleb", "mhjfbmdgcfjbbpaeojofohoefgiehjai", "nkeimhogjdpnpccoofpliimaahmaaome",
    "neajdppkdcdipfabeoofebfddakdcjhd", "pkedcjkdefgpdelpbcmbmeomcjbeemfm",
})
_RE_ID_EXTENSION: Final = re.compile(r"^[a-p]{32}$")

# Tipos de archivo de Drive y de Fotos.
_TIPOS_ARCHIVO: Final[Mapping[str, str]] = {
    **{e: "video" for e in ("mp4", "mov", "mkv", "avi", "webm", "3gp", "m4v", "wmv", "flv", "mts")},
    **{e: "imagen" for e in ("png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "jfif", "bmp", "svg", "avif",
                             "dng", "tif", "tiff", "raw", "psd")},
    **{e: "documento" for e in ("pdf", "docx", "doc", "odt", "txt", "rtf", "md", "epub")},
    **{e: "hoja de cálculo" for e in ("xlsx", "xls", "csv", "ods")},
    **{e: "presentación" for e in ("pptx", "ppt", "odp", "key")},
    **{e: "audio" for e in ("m4a", "mp3", "wav", "ogg", "opus", "flac", "aac", "wma")},
    **{e: "comprimido" for e in ("zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz")},
    **{e: "código" for e in ("py", "js", "ts", "html", "css", "json", "ipynb", "java", "c", "cpp", "cs", "php",
                             "sh", "ps1", "sql", "xml", "yml", "yaml")},
}
_TIPOS_PLURAL: Final[Mapping[str, str]] = {
    "video": "videos", "imagen": "imágenes", "documento": "documentos (PDF y texto)",
    "hoja de cálculo": "hojas de cálculo", "presentación": "presentaciones", "audio": "audios",
    "comprimido": "comprimidos", "código": "código", "otro": "otros archivos",
}

# --- Correo ---------------------------------------------------------------------------------
_RE_SEPARADOR_MBOX: Final = re.compile(
    rb"^From \S+ +(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} "
    rb"\d{1,2}:\d{2}:\d{2}(?: [+-]\d{4})? \d{4}\s*$")
_ETQ_SPAM: Final = frozenset({"spam", "correo no deseado", "junk"})
_ETQ_PAPELERA: Final = frozenset({"papelera", "trash", "bin", "eliminados", "deleted", "deleted items",
                                  "elementos eliminados"})
_ETQ_BORRADOR: Final = frozenset({"borradores", "borrador", "drafts", "draft"})
_ETQ_ENVIADO: Final = frozenset({"enviado", "enviados", "sent", "sent mail", "correo enviado", "mensajes enviados"})
_ETQ_NO_LEIDO: Final = frozenset({"no leidos", "no leido", "unread"})
_CATEGORIAS_GMAIL: Final[Mapping[str, str]] = {
    "categoria promociones": "promociones", "category promotions": "promociones",
    "categoria cambios": "actualizaciones", "categoria actualizaciones": "actualizaciones",
    "category updates": "actualizaciones",
    "categoria social": "social", "category social": "social",
    "categoria foros": "foros", "category forums": "foros",
    "categoria compras": "compras", "category purchases": "compras",
    "categoria personal": "personal", "category personal": "personal",
}
_CABECERAS_SERVICIO: Final = frozenset({"list-unsubscribe", "list-id", "feedback-id", "x-ses-outgoing",
                                        "x-campaign", "x-campaign-id", "x-mc-user", "x-mailer-sid", "x-sfmc-stack",
                                        "x-csa-complaints", "x-mailchimp-campaign"})
_PREFIJOS_SERVICIO: Final = ("x-mailgun", "x-sg-", "x-ses-", "x-sfmc", "x-mc-", "x-hubspot", "x-sendinblue")
_RE_REMITENTE_AUTOMATICO: Final = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|noresponder|no-?responder|notificacion(?:es)?|notifications?|"
    r"alerts?|alertas?|news(?:letter)?|boletin|info|mailer|marketing|updates?|hello|hola|team|equipo|support|"
    r"soporte|ayuda|help|billing|facturacion|accounts?|cuenta|security|seguridad|contacto|contact|ventas|sales|"
    r"promo(?:ciones)?|ofertas|offers|digest|mail|email|correo|bounce[s]?|postmaster|mailer-daemon|"
    r"comunicaciones|servicio(?:alcliente)?|service|customer(?:service)?)(?:[._+-].*)?$")
_DOMINIOS_CORREO_PERSONAL: Final = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.es", "outlook.com", "outlook.es", "live.com",
    "live.com.mx", "msn.com", "yahoo.com", "yahoo.es", "yahoo.com.co", "ymail.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.net", "yandex.com",
    "yandex.ru", "mail.ru", "zoho.com", "tutanota.com", "tuta.io", "hey.com", "fastmail.com",
})

SALUDOS_CORREO: Final = (
    ("cordial saludo", "Cordial saludo"), ("buenos dias", "Buenos días"), ("buenas tardes", "Buenas tardes"),
    ("buenas noches", "Buenas noches"), ("buen dia", "Buen día"), ("muy buenos dias", "Buenos días"),
    ("estimados", "Estimados"), ("estimado", "Estimado/a"), ("estimada", "Estimado/a"),
    ("apreciado", "Apreciado/a"), ("apreciada", "Apreciado/a"), ("senores", "Señores"),
    ("que mas", "Qué más"), ("quiubo", "Quiubo"), ("hola", "Hola"), ("buenas", "Buenas"),
    ("hello", "Hello"), ("hi", "Hi"), ("dear", "Dear"), ("hey", "Hey"),
)
DESPEDIDAS_CORREO: Final = (
    ("quedo atento", "Quedo atento/a"), ("quedo atenta", "Quedo atento/a"), ("quedo pendiente", "Quedo pendiente"),
    ("muchas gracias", "Muchas gracias"), ("mil gracias", "Mil gracias"), ("un saludo", "Un saludo"),
    ("un abrazo", "Un abrazo"), ("feliz dia", "Feliz día"), ("hasta pronto", "Hasta pronto"),
    ("cordialmente", "Cordialmente"), ("atentamente", "Atentamente"), ("saludos", "Saludos"),
    ("abrazos", "Abrazos"), ("gracias", "Gracias"), ("chao", "Chao"), ("regards", "Regards"),
    ("best", "Best"), ("thanks", "Thanks"), ("cheers", "Cheers"),
)
_SALUDOS_FORMALES: Final = frozenset({"cordial saludo", "estimados", "estimado", "estimada", "apreciado",
                                      "apreciada", "senores", "dear"})
_SALUDOS_INFORMALES: Final = frozenset({"hola", "buenas", "que mas", "quiubo", "hey", "hi"})
_DESPEDIDAS_FORMALES: Final = frozenset({"quedo atento", "quedo atenta", "quedo pendiente", "cordialmente",
                                         "atentamente", "regards"})
_DESPEDIDAS_INFORMALES: Final = frozenset({"un abrazo", "abrazos", "chao", "cheers"})
_TRATO_USTED: Final = frozenset({"usted", "ustedes"})
_TRATO_TU: Final = frozenset({"tu", "te", "contigo", "ti", "vos"})
_RE_CITA_ESCRIBIO: Final = re.compile(
    r"(?im)^[ \t>]*(?:el|on|le|am)\s[^\n]{0,240}?(?:\n[^\n]{0,240}?)?(?:escribi[oó]|wrote|a [ée]crit|schrieb)\s*:\s*$")
_RE_CORTE_CORREO: Final = re.compile(
    r"(?im)^[ \t]*(?:-{2,}\s*(?:original message|mensaje original|forwarded message|mensaje reenviado)\s*-{2,}"
    r"|_{5,}\s*$|-- ?$|(?:de|from)\s*:\s.*\n\s*(?:enviado|sent|date|fecha)\s*:"
    r"|enviado desde mi\b.*|sent from my\b.*|obtener outlook para\b.*|get outlook for\b.*)")
_RE_LINEA_CITA: Final = re.compile(r"(?m)^[ \t]*>.*$")
_RE_RISA_CORREO: Final = re.compile(r"\b(?:a?ja(?:ja)+j?|je(?:je)+|xd+|lol)\b", re.I)

# Archivos de las fuentes que nunca se abren (solo se cuentan).
_CLAVES_FUENTES: Final = ("cuenta", "historial", "favoritos", "extensiones", "gemini", "drive", "fotos", "correo")
_NOMBRES_FUENTE: Final[Mapping[str, str]] = {
    "cuenta": "Cuenta de Google", "historial": "historial de Chrome", "favoritos": "favoritos de Chrome",
    "extensiones": "extensiones de Chrome", "gemini": "Gemini", "drive": "Drive (solo nombres)",
    "fotos": "Google Fotos (solo fechas)", "correo": "Gmail",
}


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------


def _norm(texto: Any) -> str:
    """Minúsculas, sin tildes, espacio duro → espacio y espacios colapsados."""
    if not isinstance(texto, str):
        texto = "" if texto is None else str(texto)
    if texto.isascii():                      # atajo: nada que quitar (la mayoría de los títulos)
        return _RE_ESPACIOS.sub(" ", texto.lower()).strip()
    texto = unicodedata.normalize("NFC", texto).replace(" ", " ").replace(" ", " ")
    return _RE_ESPACIOS.sub(" ", _sin_tildes(texto)).strip()


def _lista_y(items: Sequence[str]) -> str:
    """``"a, b y c"``."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " y " + items[-1]


def _hash_corto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _tokens_lexico(texto: str) -> list[str]:
    """Tokens alfanuméricos del texto normalizado (para el léxico), sin URLs ni correos."""
    return _RE_TOKEN_LEXICO.findall(_norm(_texto_tokenizable(texto)))


def _categorias(tokens: Sequence[str]) -> tuple[set[str], set[str]]:
    """``(intereses, sensibles)`` que nombra una secuencia de tokens normalizados."""
    intereses: set[str] = set()
    sensibles: set[str] = set()
    for token in set(tokens):
        sensible = _categoria_sensible_token(token)
        if sensible is not None:
            sensibles.add(sensible)
        if token in _PALABRAS_INTERES:
            intereses.add(_PALABRAS_INTERES[token])
    if len(tokens) > 1:
        texto = f" {' '.join(tokens)} "
        for frase, categoria in _FRASES_SENSIBLES:
            if frase in texto:
                sensibles.add(categoria)
        for frase, categoria in _FRASES_INTERES:
            if frase in texto:
                intereses.add(categoria)
    return intereses, sensibles


def _sensibles_de(texto: str) -> set[str]:
    return _categorias(_tokens_lexico(texto))[1]


def _palabra_candidata(norm: str, excluir: frozenset[str] | set[str]) -> bool:
    """¿Es una palabra «útil» (latina, ≥ 4 letras, sin vocabulario funcional, nombres ni sensibles)?

    Solo decide si un nombre de archivo es automático; las palabras clave salen del vocabulario
    cerrado (:func:`_terminos_vocabulario`).
    """
    return (len(norm) >= 4 and _RE_SOLO_LATINO.fullmatch(norm) is not None and norm not in STOPWORDS_ES
            and norm not in _STOPWORDS_EN and norm not in _GENERICAS and norm not in _NOMBRES_PROPIOS_COMUNES
            and norm not in excluir and _categoria_sensible_token(norm) is None and len(set(norm)) > 1)


def _numero_largo(texto: str) -> bool:
    """¿Lleva un número de ≥ 7 dígitos, también con separadores (teléfonos, cédulas)?"""
    return _RE_DIGITOS_LARGOS.search(_RE_SEPARADOR_DIGITOS.sub("", texto)) is not None


def _etiqueta_segura(valor: Any, maximo: int = MAX_ETIQUETA) -> str:
    """Cadena corta para el perfil: sin control, URLs, correos ni números largos (si no, ``""``)."""
    if not isinstance(valor, str):
        return ""
    texto = _RE_ESPACIOS.sub(" ", _RE_CONTROL.sub(" ", valor)).strip()
    if not texto or len(texto) > maximo or _RE_URL_O_CORREO.search(texto) or _numero_largo(texto):
        return ""
    return texto


def _terminos_vocabulario(texto: str) -> list[tuple[str, str]]:
    """``[(término canónico, forma vista), …]`` del vocabulario cerrado que aparecen en un texto.

    Las frases del léxico («inteligencia artificial», «steins gate») ganan a sus palabras sueltas:
    nunca salen «inteligencia» y «artificial» por separado.
    """
    superficie = [t.lower() for t in _RE_TOKEN_SUPERFICIE.findall(_texto_tokenizable(texto))]
    normal = [_token_normal(t) for t in superficie]
    salida: list[tuple[str, str]] = []
    i = 0
    while i < len(normal):
        for largo in range(min(_MAX_FRASE, len(normal) - i), 1, -1):
            frase = _VOCAB_FRASES.get(tuple(normal[i:i + largo]))
            if frase is not None:
                salida.append((_CANONICA.get(frase, frase), " ".join(superficie[i:i + largo])))
                i += largo
                break
        else:
            palabra = normal[i]
            if palabra in _VOCAB_PALABRAS:
                salida.append((_CANONICA.get(palabra, palabra), superficie[i]))
            i += 1
    return salida


@functools.lru_cache(maxsize=65536)
def _token_normal(token: str) -> str:
    """``_sin_tildes`` de un token, con caché (los títulos repiten mucho las mismas palabras)."""
    return _sin_tildes(token)


@functools.lru_cache(maxsize=4096)
def _termino_apto(termino: str) -> bool:
    """¿Puede un término del vocabulario ser palabra clave (sin contar los nombres del correo)?"""
    if len(termino) < 3 or termino in _NO_CLAVE or termino in _GENERICAS:
        return False
    for parte in termino.split():
        if parte in _NOMBRES_PROPIOS_COMUNES or parte in _TOPONIMOS or _categoria_sensible_token(parte) is not None:
            return False
    return not _sensibles_de(termino)


def _clave_permitida(termino: str, excluir: frozenset[str] | set[str]) -> bool:
    """¿Puede un término del vocabulario ser palabra clave? Nunca nombres (comunes o de las cabeceras
    del correo), lugares, genéricos ni categorías especiales."""
    return _termino_apto(termino) and not any(parte in excluir for parte in termino.split())


@functools.lru_cache(maxsize=1)
def _vocabulario_etiquetas() -> frozenset[str]:
    """Palabras con las que puede formarse el nombre de una extensión o de un Gem."""
    palabras: set[str] = set(_VOCAB_PALABRAS) | set(_PALABRAS_HERRAMIENTA) | set(STOPWORDS_ES)
    palabras |= _STOPWORDS_EN | _GENERICAS | set(_PALABRAS_SENSIBLES)
    for frase in (*_VOCAB_FRASES, *(tuple(f.strip().split()) for f, _ in _FRASES_SENSIBLES)):
        palabras.update(frase)
    for dominio in _DOMINIOS_CONOCIDOS:
        palabras |= _etiquetas_dominio(dominio)
    for nombre in _EXTENSIONES_CONOCIDAS.values():
        palabras.update(_sin_tildes(t) for t in _RE_PALABRA_LETRAS.findall(nombre))
    return frozenset(palabras - _NOMBRES_PROPIOS_COMUNES)   # «Asistente de Gustavo» nunca califica


def _etiqueta_de_vocabulario(texto: str) -> bool:
    """¿Todas las palabras del nombre son del vocabulario (y lleva como mucho 2 dígitos)?

    Un nombre de extensión o de Gem puede ser cualquier cosa («Panel de <cliente>», «Bot 300-…»):
    solo se guarda si se forma con palabras conocidas, nunca con nombres propios desconocidos.
    """
    if sum(c.isdigit() for c in texto) > MAX_DIGITOS_ETIQUETA:
        return False
    vocabulario = _vocabulario_etiquetas()
    tokens = [_sin_tildes(t) for t in _RE_TOKEN_SUPERFICIE.findall(_RE_CAMEL.sub(" ", texto))]
    con_letras = [t for t in tokens if not t.isdigit()]
    return bool(con_letras) and all(t in vocabulario for t in con_letras)


def _tokens_nombre(texto: str) -> set[str]:
    """Tokens normalizados (≥ 3 letras) de un nombre o una etiqueta, para compararlos con exclusiones."""
    return {t for t in (_sin_tildes(p) for p in _RE_PALABRA_LETRAS.findall(_RE_CAMEL.sub(" ", texto))) if len(t) >= 3}


# ---------------------------------------------------------------------------
# Dominios
# ---------------------------------------------------------------------------


def dominio_registrable(host: Any) -> str | None:
    """Dominio registrable de un host (``mail.google.com`` → ``google.com``; ``x.com.co`` → ``x.com.co``).

    Los subdominios de plataformas con usuarios (``alguien.github.io``) se colapsan a la plataforma
    (``github.io``). ``None`` para IPs, ``localhost``, hosts sin punto o no públicos.
    """
    if not isinstance(host, str) or len(host) > 253:
        return None
    return _registrable(host.strip().lower().rstrip("."))


@functools.lru_cache(maxsize=8192)
def _registrable(host: str) -> str | None:
    if not host or "." not in host or host == "localhost":
        return None
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        pass
    if host.endswith(_SUFIJOS_NO_PUBLICOS):
        return None
    for sufijo in _SUFIJOS_PLATAFORMA:
        if host == sufijo or host.endswith("." + sufijo):
            return sufijo
    etiquetas = host.split(".")
    if ".".join(etiquetas[-2:]) in _SUFIJOS_DOBLES:
        registrable = ".".join(etiquetas[-3:]) if len(etiquetas) >= 3 else None
    else:
        registrable = ".".join(etiquetas[-2:])
    if registrable is None or not _RE_DOMINIO_VALIDO.fullmatch(registrable):
        return None
    return registrable


@functools.lru_cache(maxsize=8192)
def _sensible_de_dominio(dominio: str) -> str | None:
    """Categoría especial de un dominio registrable (o ``None``)."""
    if dominio in _ESTADO_PERMITIDOS:
        return None
    etiquetas = dominio.split(".")
    if any(f".{e}" in _SUFIJOS_ESTADO for e in etiquetas[1:]):
        return "legal y trámites"
    partes = [p for p in etiquetas[0].split("-") if p]
    plano = "".join(partes)
    for categoria, (subcadenas, exactas, prefijos) in _SENSIBLE_DOMINIO.items():
        if (any(s in plano for s in subcadenas) or plano in exactas or any(p in exactas for p in partes)
                or (prefijos and (plano.startswith(prefijos) or any(p.startswith(prefijos) for p in partes)))):
            return categoria
    for parte in (plano, *partes):
        categoria = _categoria_sensible_token(parte)
        if categoria is not None:
            return categoria
    return None


def _dominio_nombrable(dominio: str, incluir_sensibles: bool) -> bool:
    """¿Se puede nombrar este dominio en el perfil? Solo los conocidos y no sensibles; con
    ``incluir_sensibles``, también los de categorías especiales (el usuario lo pidió)."""
    sensible = _sensible_de_dominio(dominio)
    if sensible is not None:
        return incluir_sensibles
    return dominio in _DOMINIOS_CONOCIDOS


@dataclass(frozen=True)
class _Url:
    """Lo único que se deriva de una URL (nunca se guarda la URL, la ruta ni los parámetros)."""

    host: str
    registrable: str
    busqueda: bool
    servicio_google: str | None
    mapa: bool = False              # Google Maps, Waze…: el título es un lugar
    webmail: bool = False           # Roundcube, Horde… en un dominio propio: el título es un asunto
    infraestructura: bool = False   # anuncios, redirecciones, CDNs e inicio de sesión


def _analizar_url(url: Any) -> _Url | None:
    """``http(s)`` público, sin puerto explícito, IP literal ni ``localhost``; si no, ``None``."""
    if not isinstance(url, str) or len(url) > 8192:
        return None
    try:
        partes = urlsplit(url.strip())
        if partes.scheme.lower() not in ("http", "https"):
            return None
        host = (partes.hostname or "").lower()
        if partes.port is not None:
            return None
    except ValueError:
        return None
    registrable = dominio_registrable(host)
    if registrable is None:
        return None
    try:
        claves = {k.lower() for k, _ in parse_qsl(partes.query, keep_blank_values=True)}
    except ValueError:
        claves = set()
    ruta = partes.path or "/"
    busqueda = bool(claves & _CLAVES_BUSQUEDA) or _RE_RUTA_BUSQUEDA.search(ruta) is not None
    es_google = registrable == "google.com" or registrable.startswith("google.")
    mapa = (host.startswith("maps.") or registrable in _DOMINIOS_MAPAS
            or (es_google and ruta.lower().startswith("/maps")))
    webmail = (host.split(".", 1)[0] in _PREFIJOS_WEBMAIL or bool(claves & _CLAVES_WEBMAIL)
               or "/webmail" in ruta.lower())
    infraestructura = registrable in _DOMINIOS_INFRAESTRUCTURA or host.startswith(_PREFIJOS_HOST_INFRA)
    servicio = _SERVICIOS_GOOGLE.get(host)
    if servicio is None and es_google:
        if ruta.lower().startswith("/maps"):
            servicio = "Google Maps"
        elif ruta.startswith("/search"):
            servicio = "Búsqueda de Google"
    return _Url(host, registrable, busqueda, servicio, mapa, webmail, infraestructura)


def _modo_titulo(url: _Url, titulo: str = "") -> str:
    """``ignorar`` (correo, webmail, chats, documentos, mapas…), ``categorias`` (búsquedas, chats de
    IA y forjas de código) o ``completo``."""
    if (url.mapa or url.webmail or url.host in _HOSTS_TITULO_IGNORADO
            or url.registrable in _DOMINIOS_TITULO_IGNORADO or _RE_TITULO_WEBMAIL.search(titulo or "")):
        return "ignorar"
    if (url.busqueda or url.host in _HOSTS_SOLO_CATEGORIAS or url.registrable in _DOMINIOS_SOLO_CATEGORIAS):
        return "categorias"
    return "completo"


def _limpiar_titulo(titulo: str, url: _Url | None = None) -> str:
    """Título sin el contador de notificaciones («(3) …»), sin el nombre del sitio al final
    («… - YouTube», «… | Next.js», «… - Stack Overflow») y sin ``dueño/repo`` ni rutas."""
    texto = _RE_CONTADOR_TITULO.sub("", titulo)
    segmentos = _RE_SEPARADOR_SITIO.split(texto)
    if len(segmentos) > 1 and url is not None:
        ultimo = re.sub(r"[^a-z0-9]+", "", _norm(segmentos[-1]))
        marcas = {e for e in re.split(r"[.\-]", url.registrable) if len(e) >= 3 and e not in _TLD_GENERICOS}
        marcas |= {e for e in re.split(r"[.\-]", url.host) if len(e) >= 4 and e not in _TLD_GENERICOS}
        if len(ultimo) >= 3 and len(segmentos[-1].split()) <= 5 and any(m in ultimo or ultimo in m for m in marcas):
            texto = " - ".join(segmentos[:-1])
    return _RE_ESPACIOS.sub(" ", _RE_DUENO_REPO.sub(" ", texto)).strip()


def _etiquetas_dominio(dominio: str) -> set[str]:
    """Etiquetas significativas de un dominio (``youtube.com`` → ``{"youtube"}``)."""
    return {e for e in re.split(r"[.\-]", dominio) if len(e) >= 3 and e not in _TLD_GENERICOS}


# ---------------------------------------------------------------------------
# Tiempo y ritmo
# ---------------------------------------------------------------------------

_ZONA_RESPALDO: Final = timezone(timedelta(hours=-5), "COT")
_ANIO_MINIMO: Final = 1995


def _resolver_zona(zona: tzinfo | str | None) -> tzinfo:
    """``tzinfo`` tal cual; un nombre IANA con ``ZoneInfo`` (sin base de zonas, UTC−5)."""
    if isinstance(zona, tzinfo):
        return zona
    nombre = zona if isinstance(zona, str) and zona.strip() else "America/Bogota"
    try:
        return ZoneInfo(nombre.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        log.warning("Zona horaria no disponible (%s); uso UTC−5 (COT)", type(exc).__name__)
        return _ZONA_RESPALDO


def _local(segundos: float, zona: tzinfo) -> datetime | None:
    """Epoch (s) → ``datetime`` local; ``None`` si es absurdo (antes de 1995 o en el futuro)."""
    try:
        if not math.isfinite(segundos):
            return None
        momento = datetime.fromtimestamp(segundos, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if momento.year < _ANIO_MINIMO or momento > datetime.now(UTC) + timedelta(days=2):
        return None
    return momento.astimezone(zona)


class _Ritmo:
    """Histograma de horas y días de la semana (hora local) de una fuente."""

    def __init__(self) -> None:
        self.horas = [0] * 24
        self.dias = [0] * 7
        self.fechas: set[Any] = set()
        self.anios: Counter[int] = Counter()
        self.n = 0
        self.primero: datetime | None = None
        self.ultimo: datetime | None = None

    def agregar(self, momento: datetime | None) -> None:
        if momento is None:
            return
        self.n += 1
        self.horas[momento.hour] += 1
        self.dias[momento.weekday()] += 1
        self.fechas.add(momento.date())
        self.anios[momento.year] += 1
        if self.primero is None or momento < self.primero:
            self.primero = momento
        if self.ultimo is None or momento > self.ultimo:
            self.ultimo = momento

    def fusionar(self, otro: _Ritmo) -> None:
        for i, v in enumerate(otro.horas):
            self.horas[i] += v
        for i, v in enumerate(otro.dias):
            self.dias[i] += v
        self.fechas |= otro.fechas
        self.anios.update(otro.anios)
        self.n += otro.n
        for momento in (otro.primero, otro.ultimo):
            if momento is not None:
                if self.primero is None or momento < self.primero:
                    self.primero = momento
                if self.ultimo is None or momento > self.ultimo:
                    self.ultimo = momento

    def a_dict(self, *, con_anios: bool = False) -> dict[str, Any]:
        if not self.n:
            return {"eventos": 0}
        max_h = max(self.horas) or 1
        max_d = max(self.dias) or 1
        hora_pico = self.horas.index(max(self.horas))
        dia_pico = self.dias.index(max(self.dias))
        datos: dict[str, Any] = {
            "eventos": self.n,
            "dias_activos": len(self.fechas),
            "rango": [self.primero.strftime("%Y-%m"), self.ultimo.strftime("%Y-%m")]
            if self.primero and self.ultimo else [],
            "histograma_horas": [round(v / max_h, 3) for v in self.horas],
            "dias_semana": [round(v / max_d, 3) for v in self.dias],
            "hora_pico": hora_pico,
            "franja": _franja(hora_pico),
            "dia_pico": _DIAS[dia_pico],
        }
        if con_anios:
            datos["por_anio"] = {str(a): n for a, n in sorted(self.anios.items())}
            datos["anio_pico"] = max(sorted(self.anios), key=lambda a: self.anios[a]) if self.anios else None
        return datos


# ---------------------------------------------------------------------------
# Lectura dentro de los zips
# ---------------------------------------------------------------------------


@dataclass
class _Entrada:
    """Un archivo dentro de un zip (nunca se extrae a disco)."""

    zf: zipfile.ZipFile
    info: zipfile.ZipInfo
    partes: tuple[str, ...]   # componentes tras ``Takeout/<Producto>/``

    @property
    def nombre(self) -> str:
        return self.partes[-1] if self.partes else ""


def _leer_bytes(entrada: _Entrada, maximo: int) -> bytes:
    if entrada.info.file_size > maximo:
        raise ValueError("archivo demasiado grande para leerlo")
    with entrada.zf.open(entrada.info) as flujo:
        datos = flujo.read(maximo + 1)
    if len(datos) > maximo:
        raise ValueError("archivo demasiado grande para leerlo")
    return datos


def _leer_texto(entrada: _Entrada, maximo: int) -> str:
    datos = _leer_bytes(entrada, maximo)
    for codificacion in ("utf-8-sig", "utf-8"):
        try:
            return datos.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return datos.decode("latin-1")


def _leer_json(entrada: _Entrada, maximo: int) -> Any:
    return json.loads(_leer_texto(entrada, maximo))


class _ColectorHTML(HTMLParser):
    """Texto de las etiquetas pedidas (``li``, ``a``, ``h1``…) con el ``href`` de los enlaces.

    Tolera HTML sin cerrar (Netscape: ``<DT>`` y ``<p>`` nunca se cierran; un ``<li>`` abre el
    siguiente sin cerrar el anterior).
    """

    _IMPLICITAS: Final = frozenset({"li", "dt", "p", "td", "tr"})

    def __init__(self, etiquetas: Iterable[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._etiquetas = frozenset(etiquetas)
        self._pila: list[tuple[str, dict[str, str], list[str]]] = []
        self.bloques: list[tuple[str, dict[str, str], str]] = []
        self._ignorar = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._ignorar += 1
            return
        if tag not in self._etiquetas:
            return
        if tag in self._IMPLICITAS and self._pila and self._pila[-1][0] == tag:
            self._cerrar(len(self._pila) - 1)
        atributos = {k: (v or "") for k, v in attrs if k == "href"}
        self._pila.append((tag, atributos, []))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._ignorar = max(0, self._ignorar - 1)
            return
        for i in range(len(self._pila) - 1, -1, -1):
            if self._pila[i][0] == tag:
                self._cerrar(i)
                return

    def handle_data(self, data: str) -> None:
        if self._ignorar:
            return
        for _, _, partes in self._pila:
            partes.append(data)

    def _cerrar(self, indice: int) -> None:
        while len(self._pila) > indice:
            tag, atributos, partes = self._pila.pop()
            self.bloques.append((tag, atributos, _RE_ESPACIOS.sub(" ", "".join(partes)).strip()))

    def close(self) -> None:
        super().close()
        self._cerrar(0)


def _bloques_html(texto: str, etiquetas: Iterable[str]) -> list[tuple[str, dict[str, str], str]]:
    colector = _ColectorHTML(etiquetas)
    colector.feed(texto)
    colector.close()
    return colector.bloques


class _TextoHTML(HTMLParser):
    """Texto visible de un HTML (cuerpo de un correo propio sin ``text/plain``)."""

    _BLOQUES: Final = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._ignorar = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._ignorar += 1
        elif tag in self._BLOQUES:
            self.partes.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._ignorar = max(0, self._ignorar - 1)
        elif tag in self._BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignorar:
            self.partes.append(data)


def _texto_de_html(texto: str) -> str:
    lector = _TextoHTML()
    try:
        lector.feed(texto)
        lector.close()
    except Exception as exc:  # HTML imposible: se descarta ese cuerpo
        log.debug("HTML ilegible en un correo propio (%s)", type(exc).__name__)
        return ""
    return "".join(lector.partes)


# ---------------------------------------------------------------------------
# Índice de zips (deduplicado)
# ---------------------------------------------------------------------------


@dataclass
class _Indice:
    """Entradas por fuente y estadísticas del índice (sin nombres de archivo)."""

    fuentes: dict[str, list[_Entrada]] = field(default_factory=lambda: {c: [] for c in _CLAVES_FUENTES})
    zips: int = 0
    zips_duplicados: int = 0
    zips_ilegibles: int = 0
    entradas: int = 0
    entradas_duplicadas: int = 0
    excluidos: int = 0
    productos_ignorados: int = 0
    partes_faltantes: list[str] = field(default_factory=list)


def _huella_zip(tamano: int, infos: Sequence[zipfile.ZipInfo]) -> str:
    """Huella de contenido barata: nombres, CRC y tamaños del directorio central (sin leer datos)."""
    h = hashlib.sha256(str(tamano).encode())
    for info in sorted(infos, key=lambda i: i.filename):
        h.update(f"{info.filename}\0{info.CRC}\0{info.file_size}\n".encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def _partes_faltantes(nombres: Iterable[str]) -> list[str]:
    """Partes numeradas que faltan por grupo (``takeout-…-3-009.zip`` → ``"3-009"``)."""
    grupos: dict[tuple[str, str], set[int]] = {}
    for nombre in nombres:
        coincidencia = _RE_PARTE.match(nombre.strip())
        if coincidencia:
            clave = (coincidencia.group("sello").lower(), coincidencia.group("grupo"))
            grupos.setdefault(clave, set()).add(int(coincidencia.group("num")))
    faltan: list[str] = []
    for (_, grupo), numeros in sorted(grupos.items(), key=lambda par: (par[0][1], par[0][0])):
        for numero in range(1, max(numeros) + 1):
            if numero not in numeros:
                faltan.append(f"{grupo}-{numero:03d}")
    return faltan


def _clasificar(indice: _Indice, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    """Asigna una entrada a su fuente o la cuenta como excluida (sin abrirla)."""
    partes = [p for p in info.filename.split("/") if p]
    if "Takeout" in partes[:2]:
        posicion = partes.index("Takeout")
        partes = partes[posicion + 1:]
    if len(partes) < 2:
        indice.productos_ignorados += 1
        return
    producto = _PRODUCTOS.get(_norm(partes[0]))
    resto = tuple(partes[1:])
    nombre = _norm(resto[-1])
    entrada = _Entrada(zf, info, resto)
    if producto == "chrome":
        clave = _CHROME_PERMITIDOS.get(nombre)
        if clave is None:
            indice.excluidos += 1            # autocompletar, diccionario, configuración, dispositivo…
        else:
            indice.fuentes[clave].append(entrada)
    elif producto == "cuenta":
        if nombre.endswith(".subscriberinfo.html"):
            indice.fuentes["cuenta"].append(entrada)
        else:
            indice.excluidos += 1            # ChangeHistory y demás
    elif producto == "gemini":
        if nombre in _GEMINI_PERMITIDOS:
            indice.fuentes["gemini"].append(entrada)
        else:
            indice.excluidos += 1            # acciones programadas
    elif producto == "fotos":
        if len(resto) >= 2 and _norm(resto[0]) in _CARPETAS_PAPELERA:
            indice.excluidos += 1
        elif nombre.endswith(".json"):
            indice.fuentes["fotos"].append(entrada)      # los medios (fotos y videos) nunca se abren
    elif producto == "drive":
        indice.fuentes["drive"].append(entrada)    # solo el nombre: nunca se abre
    elif producto == "correo":
        if nombre.endswith(".mbox"):
            indice.fuentes["correo"].append(entrada)
        else:
            indice.excluidos += 1            # direcciones bloqueadas y configuración
    elif producto == "contactos":
        indice.excluidos += 1
    else:
        indice.productos_ignorados += 1


def _indexar(zips: Sequence[Path | str], pila: ExitStack, avisar: Progreso) -> _Indice:
    indice = _Indice()
    rutas_vistas: set[str] = set()
    huellas: set[str] = set()
    entradas_vistas: set[tuple[str, int, int]] = set()
    nombres: list[str] = []
    for crudo in zips:
        ruta = Path(crudo)
        try:
            clave_ruta = str(ruta.resolve())
        except OSError:
            clave_ruta = str(ruta.absolute())
        if clave_ruta in rutas_vistas:
            indice.zips_duplicados += 1
            continue
        rutas_vistas.add(clave_ruta)
        try:
            tamano = ruta.stat().st_size
            zf = zipfile.ZipFile(ruta)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError) as exc:
            indice.zips_ilegibles += 1
            log.warning("Un zip no se pudo abrir (%s); se ignora", type(exc).__name__)
            continue
        try:
            infos = zf.infolist()
            huella = _huella_zip(tamano, infos)
        except Exception as exc:
            zf.close()
            indice.zips_ilegibles += 1
            log.warning("El índice de un zip es ilegible (%s); se ignora", type(exc).__name__)
            continue
        if huella in huellas:
            zf.close()
            indice.zips_duplicados += 1
            continue
        huellas.add(huella)
        pila.enter_context(zf)
        indice.zips += 1
        nombres.append(ruta.name)
        for info in infos:
            if info.is_dir():
                continue
            clave = (info.filename, info.CRC, info.file_size)
            if clave in entradas_vistas:
                indice.entradas_duplicadas += 1
                continue
            entradas_vistas.add(clave)
            indice.entradas += 1
            _clasificar(indice, zf, info)
    indice.partes_faltantes = _partes_faltantes(nombres)
    avisar(f"Índice listo: {indice.zips} zips, {indice.entradas} archivos "
           f"({indice.zips_duplicados} zips duplicados ignorados).")
    return indice


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------


@dataclass
class _Contexto:
    zona: tzinfo
    incluir_sensibles: bool
    avisar: Progreso
    filtrados_sensibles: int = 0


def _procesar_cuenta(entradas: Sequence[_Entrada], ctx: _Contexto) -> dict[str, Any]:
    """Solo el ``<li>`` «Given Name: …» de ``SubscriberInfo.html``; todo lo demás se descarta."""
    patron = re.compile(r"^\s*(?:given name|nombre de pila)\s*:\s*([^:]{1,60})$", re.I)
    for entrada in entradas:
        for tag, _, texto in _bloques_html(_leer_texto(entrada, MAX_HTML_BYTES), ("li",)):
            coincidencia = patron.match(texto)
            if not coincidencia:
                continue
            nombre = " ".join(coincidencia.group(1).split())
            if re.fullmatch(r"[^\W\d_]+(?:[ '\-][^\W\d_]+){0,3}", nombre) and len(nombre) <= 40:
                return {"nombre_pila": nombre, "elementos": 1}
    return {"nombre_pila": None, "elementos": 0}


@dataclass
class _ResHistorial:
    entradas: int = 0
    excluidas: int = 0
    visitas: Counter[str] = field(default_factory=Counter)
    dias: dict[str, set[Any]] = field(default_factory=dict)
    servicios_google: Counter[str] = field(default_factory=Counter)
    ritmo: _Ritmo = field(default_factory=_Ritmo)
    # título normalizado → [título original, modo, días, dominio]; solo en memoria.
    titulos: dict[str, list[Any]] = field(default_factory=dict)
    filtrados: int = 0


_ORDEN_MODO: Final = {"completo": 0, "categorias": 1, "ignorar": 2}


def _procesar_historial(entradas: Sequence[_Entrada], ctx: _Contexto) -> _ResHistorial:
    res = _ResHistorial()
    for entrada in entradas:
        ctx.avisar("Chrome: leyendo el historial…")
        datos = _leer_json(entrada, MAX_JSON_BYTES)
        lista = datos.get("Browser History") if isinstance(datos, dict) else None
        if not isinstance(lista, list):
            raise ValueError("el historial no tiene la lista 'Browser History'")
        for item in lista:
            if not isinstance(item, dict):
                continue
            res.entradas += 1
            url = _analizar_url(item.get("url"))
            if url is None:
                res.excluidas += 1           # localhost, IP, puerto, chrome-extension:…
                continue
            momento = None
            micro = item.get("time_usec")
            if isinstance(micro, (int, float)) and not isinstance(micro, bool):
                momento = _local(micro / 1_000_000, ctx.zona)
            res.ritmo.agregar(momento)
            if _sensible_de_dominio(url.registrable) and not ctx.incluir_sensibles:
                res.filtrados += 1
                continue
            if url.servicio_google:
                res.servicios_google[url.servicio_google] += 1
            if not url.infraestructura:      # anuncios, redirecciones e inicio de sesión: solo ritmo
                res.visitas[url.registrable] += 1
                if momento is not None:
                    res.dias.setdefault(url.registrable, set()).add(momento.date())
            titulo = item.get("title")
            if not isinstance(titulo, str) or not titulo.strip() or len(titulo) > 500:
                continue
            modo = _modo_titulo(url, titulo)
            if modo == "ignorar":
                continue
            limpio = _limpiar_titulo(titulo, url)
            clave = _norm(limpio)
            if not clave:
                continue
            registro = res.titulos.get(clave)
            if registro is None:
                registro = [limpio, modo, set(), url.registrable]
                res.titulos[clave] = registro
            elif _ORDEN_MODO[modo] > _ORDEN_MODO[registro[1]]:
                registro[1] = modo           # el modo más restrictivo gana
            if momento is not None:
                registro[2].add(momento.date())
        del datos, lista
    return res


@dataclass
class _ResFavoritos:
    elementos: int = 0
    dominios: Counter[str] = field(default_factory=Counter)
    docs: list[tuple[str, str]] = field(default_factory=list)   # (texto, modo)
    filtrados: int = 0


def _procesar_favoritos(entradas: Sequence[_Entrada], ctx: _Contexto) -> _ResFavoritos:
    res = _ResFavoritos()
    carpetas_fijas = {"bookmarks", "bookmarks bar", "other bookmarks", "mobile bookmarks", "barra de favoritos",
                      "otros favoritos", "favoritos", "favoritos del movil", "reading list", "lista de lectura"}
    for entrada in entradas:
        for tag, atributos, texto in _bloques_html(_leer_texto(entrada, MAX_HTML_BYTES), ("a", "h3")):
            if tag == "h3":
                if texto and _norm(texto) not in carpetas_fijas:
                    res.docs.append((_limpiar_titulo(texto), "completo"))
                continue
            url = _analizar_url(atributos.get("href"))
            if url is None:
                continue
            res.elementos += 1
            if _sensible_de_dominio(url.registrable) and not ctx.incluir_sensibles:
                res.filtrados += 1
                continue
            if not url.infraestructura:
                res.dominios[url.registrable] += 1
            modo = _modo_titulo(url, texto)
            if texto and modo != "ignorar":
                res.docs.append((_limpiar_titulo(texto, url), modo))
    return res


def _procesar_extensiones(entradas: Sequence[_Entrada], ctx: _Contexto) -> dict[str, Any]:
    nombres: list[str] = []
    sin_nombre = 0
    total = 0
    filtradas = 0
    for entrada in entradas:
        datos = _leer_json(entrada, MAX_JSON_BYTES)
        lista = datos.get("Extensions") if isinstance(datos, dict) else None
        if not isinstance(lista, list):
            raise ValueError("Extensiones.json sin la lista 'Extensions'")
        for ext in lista:
            if not isinstance(ext, dict):
                continue
            ident = str(ext.get("id") or "").strip().lower()
            if ident in _EXTENSIONES_INTERNAS:
                continue
            if ext.get("enabled") is False:
                continue
            total += 1
            # Un id conocido manda (nombre de producto curado). Si no, el ``name`` del export solo
            # vale si se forma con palabras del vocabulario: una extensión propia o de un cliente
            # puede llamarse «Panel de <persona>» o llevar un teléfono.
            nombre = _EXTENSIONES_CONOCIDAS.get(ident, "") if _RE_ID_EXTENSION.fullmatch(ident) else ""
            crudo = ext.get("name")
            if not nombre and isinstance(crudo, str) and not crudo.startswith("__MSG_"):
                candidato = _etiqueta_segura(crudo, 50)
                if candidato and _etiqueta_de_vocabulario(candidato):
                    nombre = candidato
            if not nombre:
                sin_nombre += 1
                continue
            if _sensibles_de(nombre) or ident in ("nkbihfbeogaeaoehlefnkodbefgpgknn", "bfnaelmomeimhlpmgjnjophhpkkoljpa"):
                if not ctx.incluir_sensibles:
                    filtradas += 1
                    continue
            if nombre not in nombres:
                nombres.append(nombre)
    ctx.filtrados_sensibles += filtradas
    return {"extensiones": sorted(nombres, key=str.lower)[:MAX_EXTENSIONES], "sin_nombre": sin_nombre,
            "activas": total, "elementos": total}


def _procesar_gemini(entradas: Sequence[_Entrada], ctx: _Contexto) -> dict[str, Any]:
    """Nombres de Gems: **solo** los campos etiquetados («Nombre: …», «Name: …»). Los encabezados
    (``h1``–``h4``) nunca cuentan: pueden ser la cabecera de la página («Gems») o secciones de
    instrucciones. El export real venía vacío (``<x></x>``); el lector es tolerante. Los nombres de
    personas se quitan después, en :func:`_ensamblar`, con los de las cabeceras del correo."""
    etiqueta = re.compile(r"^\s*(?:nombre|name|gem name|nombre del gem|nombre de la gem|titulo|title)\s*:\s*(.+)$",
                          re.I)
    nombres: list[str] = []
    filtrados = 0
    for entrada in entradas:
        texto = _leer_texto(entrada, MAX_HTML_BYTES)
        for _, _, bloque in _bloques_html(texto, ("li", "p", "td", "dt", "dd")):
            if not etiqueta.match(_norm(bloque)):
                continue
            nombre = _etiqueta_segura(" ".join(bloque.split(":", 1)[1].split()), 40)
            if not nombre or not _etiqueta_de_vocabulario(nombre):
                continue
            if _tokens_nombre(nombre) & _NOMBRES_PROPIOS_COMUNES:
                continue                     # podría nombrar a una persona
            if _sensibles_de(nombre) and not ctx.incluir_sensibles:
                filtrados += 1
                continue
            if nombre not in nombres:
                nombres.append(nombre)
    ctx.filtrados_sensibles += filtrados
    return {"gems": nombres[:MAX_GEMS], "n": len(nombres), "elementos": len(nombres)}


@dataclass
class _ResDrive:
    archivos: int = 0
    tipos: Counter[str] = field(default_factory=Counter)
    bytes_tipo: Counter[str] = field(default_factory=Counter)
    docs: list[str] = field(default_factory=list)
    automaticos: int = 0


def _nombre_util(nombre: str, *, es_archivo: bool) -> str | None:
    """Nombre de archivo o carpeta listo para extraer palabras (``None`` si es automático)."""
    base = nombre
    if es_archivo and "." in base:
        base = base.rsplit(".", 1)[0]
    base = _RE_COPIA_NUMERADA.sub("", base)
    base = _RE_PREFIJO_COPIA.sub("", base).strip()
    plano = _norm(base).replace(" ", "")
    if not base or _RE_HASH.fullmatch(plano.replace("-", "")) or not re.search(r"[a-z]{3,}", plano):
        return None
    texto = _RE_CAMEL.sub(" ", _RE_SEPARADORES_NOMBRE.sub(" ", base))
    utiles = [p for p in _RE_PALABRA_LETRAS.findall(texto) if _palabra_candidata(_sin_tildes(p), frozenset())]
    return texto if utiles else None


def _procesar_drive(entradas: Sequence[_Entrada], ctx: _Contexto) -> _ResDrive:
    """Solo el índice del zip (nombres y tamaños): ningún archivo de Drive se abre."""
    res = _ResDrive()
    carpetas: set[str] = set()
    for entrada in entradas:
        res.archivos += 1
        nombre = entrada.nombre
        extension = nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""
        tipo = _TIPOS_ARCHIVO.get(extension, "otro")
        res.tipos[tipo] += 1
        res.bytes_tipo[tipo] += max(0, int(entrada.info.file_size))
        util = _nombre_util(nombre, es_archivo=True)
        if util is None:
            res.automaticos += 1
        else:
            res.docs.append(util)
        for carpeta in entrada.partes[:-1]:
            if carpeta not in carpetas:
                carpetas.add(carpeta)
                util_carpeta = _nombre_util(carpeta, es_archivo=False)
                if util_carpeta is not None:
                    res.docs.append(util_carpeta)
    return res


@dataclass
class _ResFotos:
    ritmo: _Ritmo = field(default_factory=_Ritmo)
    sidecars: int = 0
    excluidas: int = 0
    ajenas: int = 0
    ilegibles: int = 0
    videos: int = 0


def _procesar_fotos(entradas: Sequence[_Entrada], ctx: _Contexto) -> _ResFotos:
    """Solo ``photoTakenTime.timestamp`` de cada ``.json``; nunca ``geoData``, ``people``,
    ``description``, ``title`` ni ``url`` (no se accede a esas claves)."""
    res = _ResFotos()
    sufijo = re.compile(r"(?:\.supplemental-metadata(?:\(\d+\))?)?\.json$", re.I)
    for i, entrada in enumerate(entradas):
        if i and i % 2000 == 0:
            ctx.avisar(f"Fotos: {i} metadatos leídos…")
        try:
            datos = _leer_json(entrada, MAX_SIDECAR_BYTES)
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            res.ilegibles += 1
            log.debug("Metadatos de una foto ilegibles (%s)", type(exc).__name__)
            continue
        if not isinstance(datos, dict) or "photoTakenTime" not in datos:
            continue                         # metadatos de álbum u otro JSON
        res.sidecars += 1
        if datos.get("trashed") is True:
            res.excluidas += 1
            continue
        origen = datos.get("googlePhotosOrigin")
        if isinstance(origen, dict) and "fromSharedAlbum" in origen:
            res.ajenas += 1                  # fotos de otras personas
            continue
        tomada = datos.get("photoTakenTime")
        crudo = tomada.get("timestamp") if isinstance(tomada, dict) else None
        del datos
        try:
            segundos = int(str(crudo).strip())
        except (TypeError, ValueError):
            continue
        if segundos <= 0:
            continue
        momento = _local(segundos, ctx.zona)
        if momento is None:
            continue
        res.ritmo.agregar(momento)
        medio = sufijo.sub("", entrada.nombre)
        extension = medio.rsplit(".", 1)[-1].lower() if "." in medio else ""
        if _TIPOS_ARCHIVO.get(extension) == "video":
            res.videos += 1
    return res


# --- Correo -----------------------------------------------------------------------------------


def _reparar_bytes(texto: str) -> str:
    """Cabecera con bytes UTF-8 crudos (``compat32`` los deja como *surrogate escapes*) → texto."""
    try:
        bruto = texto.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return texto
    try:
        return bruto.decode("utf-8")
    except UnicodeDecodeError:
        return bruto.decode("latin-1")


def _decodificar(valor: Any) -> str:
    """Cabecera RFC 2047 (o con UTF-8 crudo) → texto (si falla, el valor crudo)."""
    if valor is None:
        return ""
    crudo = _reparar_bytes(str(valor))
    if "=?" not in crudo:
        return crudo
    try:
        return str(make_header(decode_header(crudo)))
    except Exception as exc:  # charset desconocido o cabecera mal formada
        log.debug("Cabecera RFC 2047 ilegible (%s)", type(exc).__name__)
        return crudo


def _partir_etiquetas(valor: str) -> list[str]:
    """``X-Gmail-Labels`` separadas por comas, respetando las comillas."""
    salida: list[str] = []
    actual: list[str] = []
    comillas = False
    for caracter in valor:
        if caracter == '"':
            comillas = not comillas
        elif caracter == "," and not comillas:
            salida.append("".join(actual).strip())
            actual = []
        else:
            actual.append(caracter)
    if actual:
        salida.append("".join(actual).strip())
    return [s for s in salida if s]


def _cabeceras_correo(datos: bytes) -> dict[str, list[str]]:
    """Cabeceras de un mensaje (nombre en minúsculas → valores, sin plegar). El UTF-8 crudo se
    respeta; lo ilegible se reemplaza. Más rápido y predecible que ``compat32`` (que devuelve
    objetos ``Header`` ante bytes de 8 bits)."""
    cabeceras: dict[str, list[str]] = {}
    nombre: str | None = None
    actual: list[str] = []

    def guardar() -> None:
        if nombre is not None:
            cabeceras.setdefault(nombre, []).append(" ".join(actual).strip())

    for linea in datos.decode("utf-8", "replace").splitlines():
        if linea[:1] in (" ", "\t"):
            if nombre is not None:
                actual.append(linea.strip())
            continue
        guardar()
        clave, separador, valor = linea.partition(":")
        if not separador or not clave or " " in clave.strip():
            nombre, actual = None, []
            continue
        nombre, actual = clave.strip().lower(), [valor.strip()]
    guardar()
    return cabeceras


def _primera(cabeceras: Mapping[str, list[str]], nombre: str) -> str:
    valores = cabeceras.get(nombre)
    return valores[0] if valores else ""


def _hash_direccion(direccion: str) -> str:
    return hashlib.sha256(direccion.strip().lower().encode("utf-8", "surrogatepass")).hexdigest()


def _lineas_binarias(flujo: IO[bytes]) -> Iterator[tuple[bytes, bool]]:
    """``(línea, empieza_en_inicio_de_línea)``; una línea gigante se parte en trozos."""
    al_inicio = True
    while True:
        linea = flujo.readline(MAX_LINEA_MBOX)
        if not linea:
            return
        yield linea, al_inicio
        al_inicio = linea.endswith(b"\n")


def _recorrer_mbox(flujo: IO[bytes], al_cabeceras: Callable[[bytes, bytes], Any],
                   al_cerrar: Callable[[Any, bytes], None]) -> int:
    """Recorre un mbox en streaming. Por mensaje llama ``al_cabeceras(cabeceras, separador)``; si
    devuelve algo distinto de ``None``, acumula el cuerpo (≤ ``MAX_CUERPO_ENVIADO``) y llama
    ``al_cerrar(estado, cuerpo)``. Los cuerpos descartados nunca se guardan en memoria."""
    mensajes = 0
    cabeceras: list[bytes] | None = None
    tam_cabeceras = 0
    cuerpo: list[bytes] | None = None
    tam_cuerpo = 0
    estado: Any = None
    separador = b""
    abierto = False

    def cerrar() -> None:
        nonlocal estado
        if cabeceras is not None:            # mensaje sin línea en blanco: solo cabeceras
            estado = al_cabeceras(b"".join(cabeceras), separador)
            if estado is not None:
                al_cerrar(estado, b"")
        elif estado is not None and cuerpo is not None:
            al_cerrar(estado, b"".join(cuerpo))
        estado = None

    for linea, al_inicio in _lineas_binarias(flujo):
        if al_inicio and linea.startswith(b"From ") and _RE_SEPARADOR_MBOX.match(linea):
            if abierto:
                cerrar()
                mensajes += 1
            abierto = True
            separador = linea
            cabeceras, tam_cabeceras = [], 0
            cuerpo, tam_cuerpo, estado = None, 0, None
            continue
        if not abierto:
            continue
        if cabeceras is not None:
            if al_inicio and linea in (b"\r\n", b"\n"):
                estado = al_cabeceras(b"".join(cabeceras), separador)
                cabeceras = None
                cuerpo = [] if estado is not None else None
            elif tam_cabeceras < MAX_CABECERA_CORREO:
                cabeceras.append(linea)
                tam_cabeceras += len(linea)
            continue
        if cuerpo is not None and tam_cuerpo < MAX_CUERPO_ENVIADO:
            if al_inicio and linea.startswith(b">From "):
                linea = linea[1:]           # des-escapado mboxrd
            cuerpo.append(linea)
            tam_cuerpo += len(linea)
    if abierto:
        cerrar()
        mensajes += 1
    return mensajes


@dataclass
class _Enviado:
    """Un correo enviado por la cuenta (en memoria; nunca se guarda el texto)."""

    remitente: str                 # hash
    destinatarios: frozenset[str]  # hashes
    momento: datetime | None
    texto: str = ""
    excluir: frozenset[str] = frozenset()


@dataclass
class _ResCorreo:
    mensajes: int = 0
    excluidos: int = 0             # Spam y Papelera
    borradores: int = 0
    recibidos: int = 0
    sin_leer: int = 0
    categorias: Counter[str] = field(default_factory=Counter)
    entregado_a: Counter[str] = field(default_factory=Counter)
    candidatos: list[_Enviado] = field(default_factory=list)
    servicios: Counter[str] = field(default_factory=Counter)
    categoria_servicio: dict[str, Counter[str]] = field(default_factory=dict)
    remitente_servicio: dict[str, Counter[str]] = field(default_factory=dict)   # dominio → hashes de remitente
    filtrados: int = 0
    # Tokens de los nombres y direcciones de las personas de las cabeceras (remitentes que no son
    # servicios, destinatarios y el propio usuario). Solo en memoria: sirven para que ningún nombre
    # de esas personas acabe como palabra clave, extensión, Gem o dominio. Nunca se guardan.
    nombres: set[str] = field(default_factory=set)


# Prioridad fija cuando un mensaje lleva dos categorías («Categoría Compras» + «Categoría: Personal»):
# el resultado no depende del orden de un ``set`` (``PYTHONHASHSEED``).
_PRIORIDAD_CATEGORIA: Final = ("compras", "promociones", "social", "actualizaciones", "foros", "personal")
# Partes de un buzón que no son nombres de personas.
_GENERICOS_BUZON: Final = frozenset("""
info contacto contact admin administrador soporte support ventas sales noreply reply hola hello team equipo mail
email correo notificaciones notifications news newsletter cuenta account undisclosed recipients destinatarios
ocultos lista list grupo group oficina office gerencia recepcion
""".split())


def _remitente(valor: str) -> tuple[str, str]:
    """``(nombre visible, dirección)`` de un ``From``. Se parsea el valor **crudo** (las *encoded-words*
    de RFC 2047 son átomos válidos) y solo después se decodifica el nombre: decodificar antes rompe
    el parser estricto ante «Pérez, Juan» o «[Alertas] Plataforma». Si aun así falla, se busca la
    dirección entre ``<…>``."""
    nombre, direccion = parseaddr(valor)
    if "@" not in direccion:
        texto = _decodificar(valor)
        angulos = _RE_DIRECCION_ANGULOS.findall(texto)
        sueltas = _RE_DIRECCION.findall(texto)
        direccion = angulos[-1] if angulos else (sueltas[-1] if sueltas else "")
        nombre = _RE_DIRECCION.sub(" ", _RE_DIRECCION_ANGULOS.sub(" ", texto))
    else:
        nombre = _decodificar(nombre)
    return nombre.strip().strip("\"'").strip(), direccion.strip().lower()


def _destinatarios(cabeceras: Mapping[str, list[str]]) -> tuple[set[str], set[str]]:
    """``(hashes de las direcciones, tokens de los nombres)`` de ``To``/``Cc``/``Bcc``. Con una
    expresión regular sobre el valor decodificado: una coma sin comillas en un nombre no hace
    perder a nadie (el parser estricto de ``email.utils`` lo descartaría todo)."""
    direcciones: set[str] = set()
    tokens: set[str] = set()
    for clave in ("to", "cc", "bcc"):
        for valor in cabeceras.get(clave, ()):
            texto = _decodificar(valor)
            for direccion in _RE_DIRECCION.findall(texto):
                direcciones.add(_hash_direccion(direccion))
                tokens |= _tokens_persona("", direccion)
            tokens |= _tokens_persona(_RE_DIRECCION.sub(" ", texto), "")
    return direcciones, tokens


def _tokens_persona(nombre: str, direccion: str) -> set[str]:
    """Tokens (≥ 3 letras, normalizados) del nombre visible y de la parte local de la dirección."""
    tokens = _tokens_nombre(nombre)
    if direccion:
        tokens |= _tokens_nombre(direccion.split("@", 1)[0])
    return {t for t in tokens if t not in STOPWORDS_ES and t not in _STOPWORDS_EN and t not in _GENERICOS_BUZON}


def _fecha_correo(valor: Any, separador: bytes, zona: tzinfo) -> datetime | None:
    momento: datetime | None = None
    if valor:
        try:
            momento = parsedate_to_datetime(str(valor))
        except (TypeError, ValueError, IndexError, OverflowError):
            momento = None
    if momento is None and separador:
        partes = separador.decode("ascii", "replace").split()
        try:
            momento = datetime.strptime(" ".join(p for p in partes[2:] if not p.startswith(("+", "-"))),
                                        "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
        except ValueError:
            momento = None
    if momento is None:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return _local(momento.timestamp(), zona)


def _texto_propio(texto: str) -> str:
    """El texto escrito por el usuario: sin citas, reenvíos, firmas ni «Enviado desde mi…»."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    cortes = [m.start() for m in (_RE_CITA_ESCRIBIO.search(texto), _RE_CORTE_CORREO.search(texto)) if m]
    if cortes:
        texto = texto[:min(cortes)]
    texto = _RE_LINEA_CITA.sub("", texto)
    lineas = [linea.rstrip() for linea in texto.split("\n")]
    return "\n".join(lineas).strip()[:MAX_TEXTO_ENVIADO]


def _cuerpo_enviado(cabeceras: bytes, cuerpo: bytes) -> str:
    """Texto propio de un correo enviado (``text/plain`` preferido; si no, el HTML sin etiquetas)."""
    try:
        mensaje = message_from_bytes(cabeceras + b"\r\n" + cuerpo, policy=politica_correo.default)
        parte = mensaje.get_body(preferencelist=("plain", "html"))
        if parte is None:
            return ""
        contenido = parte.get_content()
    except Exception as exc:  # MIME roto o charset raro: ese correo no cuenta para el estilo
        log.debug("Cuerpo de un correo propio ilegible (%s)", type(exc).__name__)
        return ""
    if not isinstance(contenido, str):
        return ""
    if parte.get_content_subtype() == "html":
        contenido = _texto_de_html(contenido)
    return _texto_propio(contenido)


def _procesar_correo(entradas: Sequence[_Entrada], ctx: _Contexto) -> _ResCorreo:
    """mbox en streaming: etiquetas, remitentes de servicio y **solo** los cuerpos enviados."""
    res = _ResCorreo()

    def al_cabeceras(datos: bytes, separador: bytes) -> Any:
        res.mensajes += 1
        if res.mensajes % 5000 == 0:
            ctx.avisar(f"Correo: {res.mensajes} mensajes…")
        cabeceras = _cabeceras_correo(datos)
        etiquetas: set[str] = set()
        for valor in cabeceras.get("x-gmail-labels", ()):
            etiquetas.update(_RE_ESPACIOS.sub(" ", _norm(e).replace(":", " ")).strip()
                             for e in _partir_etiquetas(_decodificar(valor)))
        if etiquetas & (_ETQ_SPAM | _ETQ_PAPELERA):
            res.excluidos += 1               # Spam y Papelera: ni se miran
            return None
        for valor in cabeceras.get("delivered-to", ()):
            direccion = parseaddr(valor)[1]
            if "@" in direccion:
                res.entregado_a[_hash_direccion(direccion)] += 1
        if etiquetas & _ETQ_BORRADOR:
            res.borradores += 1
            return None
        nombre_remitente, remitente = _remitente(_primera(cabeceras, "from"))
        destinatarios, nombres_destino = _destinatarios(cabeceras)
        res.nombres |= nombres_destino        # el propio usuario y otras personas
        if etiquetas & _ETQ_ENVIADO and "@" in remitente:
            res.nombres |= _tokens_persona(nombre_remitente, remitente)   # su nombre y su apellido
            return (datos, _Enviado(_hash_direccion(remitente), frozenset(destinatarios),
                                    _fecha_correo(_primera(cabeceras, "date"), separador, ctx.zona),
                                    excluir=frozenset(nombres_destino)))
        res.recibidos += 1
        if etiquetas & _ETQ_NO_LEIDO:
            res.sin_leer += 1
        presentes = {_CATEGORIAS_GMAIL[e] for e in etiquetas if e in _CATEGORIAS_GMAIL}
        categoria = next((c for c in _PRIORIDAD_CATEGORIA if c in presentes), None)
        if categoria:
            res.categorias[categoria] += 1
        if "@" not in remitente:
            res.nombres |= _tokens_persona(nombre_remitente, "")
            return None
        local, _, dominio_correo = remitente.rpartition("@")
        dominio = dominio_registrable(dominio_correo)
        claves = set(cabeceras)
        precedencia = _primera(cabeceras, "precedence").strip().lower()
        automatico = _primera(cabeceras, "auto-submitted").strip().lower()
        servicio = (bool(claves & _CABECERAS_SERVICIO) or any(k.startswith(_PREFIJOS_SERVICIO) for k in claves)
                    or precedencia in ("bulk", "list", "junk") or (automatico not in ("", "no"))
                    or _RE_REMITENTE_AUTOMATICO.match(local) is not None)
        if dominio is None or dominio in _DOMINIOS_CORREO_PERSONAL or not servicio:
            res.nombres |= _tokens_persona(nombre_remitente, remitente)   # una persona: nunca se cuenta
            return None
        if _sensible_de_dominio(dominio) and not ctx.incluir_sensibles:
            res.filtrados += 1
            return None
        res.servicios[dominio] += 1
        res.categoria_servicio.setdefault(dominio, Counter())[categoria or "otros"] += 1
        res.remitente_servicio.setdefault(dominio, Counter())[_hash_direccion(remitente)] += 1
        return None

    def al_cerrar(estado: Any, cuerpo: bytes) -> None:
        cabeceras, enviado = estado
        enviado.texto = _cuerpo_enviado(cabeceras, cuerpo) if cuerpo else ""
        res.candidatos.append(enviado)

    for entrada in entradas:
        ctx.avisar("Correo: recorriendo el mbox en streaming…")
        crudo = entrada.zf.open(entrada.info)
        with io.BufferedReader(crudo, 1 << 20) as flujo:   # type: ignore[arg-type]
            _recorrer_mbox(flujo, al_cabeceras, al_cerrar)
    return res


_VENTANA_FIRMA: Final = 8          # líneas finales donde se busca la despedida antes de la firma
_MAX_PALABRAS_DESPEDIDA: Final = 6


def _sin_simbolos(linea: str) -> str:
    return re.sub(r"[^\w ]+", " ", _norm(linea)).strip()


def _despedida_y_corte(lineas: Sequence[str]) -> tuple[str | None, int]:
    """``(despedida, índice de la última línea propia)``. Busca, de abajo arriba y sin contar la
    primera línea (el saludo), una línea corta que empiece por una despedida («Cordialmente,»,
    «Muchas gracias») entre las últimas: lo que va detrás es la firma (nombre, cargo, teléfono,
    dirección) y no cuenta como texto propio. Sin esa línea, mira las tres últimas líneas juntas
    (correos cortos: «… Gracias»)."""
    inicio = max(1, len(lineas) - _VENTANA_FIRMA)
    for i in range(len(lineas) - 1, inicio - 1, -1):
        limpia = _sin_simbolos(lineas[i])
        if not limpia or len(limpia.split()) > _MAX_PALABRAS_DESPEDIDA:
            continue
        clave = next((c for c, _ in DESPEDIDAS_CORREO if limpia == c or limpia.startswith(c + " ")), None)
        if clave is not None:
            return clave, i
    cola = " ".join(_sin_simbolos(linea) for linea in lineas[-3:])
    clave = next((c for c, _ in DESPEDIDAS_CORREO if re.search(rf"(?:^| ){re.escape(c)}(?: |$)", cola)), None)
    return clave, len(lineas) - 1


def _texto_sin_firma(texto: str) -> tuple[str, str | None]:
    """``(texto propio sin la firma, despedida)`` de un correo ya sin citas ni reenvíos."""
    lineas = [linea for linea in texto.split("\n") if linea.strip()]
    if not lineas:
        return "", None
    despedida, ultima = _despedida_y_corte(lineas)
    return "\n".join(lineas[:ultima + 1]), despedida


def _estilo_correo(textos: Sequence[str], excluir: frozenset[str]) -> dict[str, Any]:
    """Estilo de los correos propios a terceros (como el de WhatsApp, con la confianza marcada).

    Solo cuentan los correos con ≥ ``MIN_PALABRAS_ESTILO`` palabras propias (sin citas, reenvíos
    ni firma) y hacen falta ≥ ``MIN_CORREOS_ESTILO`` (ESQUEMA §4.5): con «ok», «Adjunto» o «listo»
    no hay estilo que aprender.
    """
    validos: list[tuple[str, str | None]] = []
    for texto in textos:
        propio, despedida = _texto_sin_firma(texto)
        if len(propio.split()) >= MIN_PALABRAS_ESTILO:
            validos.append((propio, despedida))
    n = len(validos)
    if n < MIN_CORREOS_ESTILO:
        return {"suficiente": False, "mensajes": n, "confianza": "insuficiente"}
    textos = [t for t, _ in validos]
    palabras = [len(t.split()) for t in textos]
    saludos: Counter[str] = Counter()
    despedidas: Counter[str] = Counter()
    trato: Counter[str] = Counter()
    puntajes: list[int] = []
    for texto, despedida in validos:
        lineas = [_norm(linea) for linea in texto.split("\n") if linea.strip()]
        if not lineas:
            continue
        primera = re.sub(r"[^\w ]+", " ", lineas[0]).strip()
        saludo = next((clave for clave, _ in SALUDOS_CORREO
                       if primera == clave or primera.startswith(clave + " ")), None)
        if saludo:
            saludos[saludo] += 1
        if despedida:
            despedidas[despedida] += 1
        tokens = re.findall(r"[a-z]+", _norm(texto))
        usted = sum(1 for t in tokens if t in _TRATO_USTED)
        tu = sum(1 for t in tokens if t in _TRATO_TU)
        if usted > tu:
            trato["usted"] += 1
        elif tu > usted:
            trato["tú"] += 1
        puntaje = int(saludo in _SALUDOS_FORMALES) + int(despedida in _DESPEDIDAS_FORMALES) + min(usted, 1)
        puntaje -= (int(saludo in _SALUDOS_INFORMALES) + int(despedida in _DESPEDIDAS_INFORMALES)
                    + int(bool(_emojis(texto))) + int(bool(_RE_RISA_CORREO.search(texto))) + min(tu, 1))
        puntajes.append(puntaje)
    media = statistics.fmean(puntajes) if puntajes else 0.0
    formalidad = "formal" if media >= 0.75 else "informal" if media <= -0.75 else "neutra"
    total_trato = sum(trato.values())
    tratamiento: str | None = None
    if total_trato:
        dominante, veces = trato.most_common(1)[0]
        tratamiento = dominante if veces / total_trato >= 0.7 else "mixto"

    def proporcion(condicion: Callable[[str], bool]) -> float:
        return round(sum(1 for t in textos if condicion(t)) / n, 2)

    emojis: Counter[str] = Counter()
    for texto in textos:
        emojis.update(c for c in _emojis(texto) if not 0x1F1E6 <= ord(c) <= 0x1F1FF)
    muletillas = [m for m in extraer_muletillas(list(textos), [], excluir=excluir, top=8,
                                                min_freq=MIN_MULETILLA_CORREO)
                  if _es_texto_seguro(m["ngrama"], excluir)]
    nombres_saludo = dict(SALUDOS_CORREO)
    nombres_despedida = dict(DESPEDIDAS_CORREO)
    return {
        "suficiente": True,
        "mensajes": n,
        "confianza": ("baja" if n < CORREOS_CONFIANZA_MEDIA else "media" if n < CORREOS_CONFIANZA_ALTA
                      else "alta"),
        "longitud_media_palabras": round(statistics.fmean(palabras), 1),
        "longitud_mediana_palabras": int(statistics.median(palabras)),
        "saludos": list(dict.fromkeys(nombres_saludo[s] for s, _ in saludos.most_common(3))),
        "despedidas": list(dict.fromkeys(nombres_despedida[d] for d, _ in despedidas.most_common(3))),
        "muletillas": [{"ngrama": m["ngrama"], "freq": m["freq"]} for m in muletillas],
        "tratamiento": tratamiento,
        "formalidad": formalidad,
        "pct_con_emoji": proporcion(lambda t: bool(_emojis(t))),
        "emojis_top": [e for e, _ in emojis.most_common(3)],
        "pct_preguntas": proporcion(lambda t: "?" in t),
        "pct_signo_apertura": proporcion(lambda t: "¿" in t or "¡" in t),
        "pct_mayuscula_inicial": proporcion(_empieza_con_mayuscula),
    }


# ---------------------------------------------------------------------------
# Corpus de textos cortos → categorías y palabras clave
# ---------------------------------------------------------------------------


class _Corpus:
    """Documentos cortos de una fuente (títulos, nombres de archivo, Gems).

    Las palabras clave salen **solo** del vocabulario cerrado (:func:`_terminos_vocabulario`): un
    nombre de persona, un apodo o un lugar en minúscula nunca califica, por muchas veces que salga.
    """

    def __init__(self, nombre: str, *, min_docs: int, min_dias: int = 0) -> None:
        self.nombre = nombre
        self.min_docs = min_docs
        self.min_dias = min_dias
        self.n_docs = 0
        self.filtrados = 0
        self.cat_docs: Counter[str] = Counter()
        self.sensibles: set[str] = set()
        self.docs: Counter[str] = Counter()
        self.formas: dict[str, Counter[str]] = {}      # término → formas vistas (minúsculas, con tildes)
        self.dias: dict[str, set[Any]] = {}

    def agregar(self, texto: str, *, modo: str = "completo", dias: Iterable[Any] = (),
                excluir: frozenset[str] = frozenset(), incluir_sensibles: bool = False) -> None:
        intereses, sensibles = _categorias(_tokens_lexico(texto))
        self.n_docs += 1
        if sensibles and not incluir_sensibles:
            self.filtrados += 1
            return
        for categoria in intereses | sensibles:
            self.cat_docs[categoria] += 1
        self.sensibles |= sensibles
        if modo != "completo":
            return
        dias_doc = set(dias)
        vistos: set[str] = set()
        for termino, forma in _terminos_vocabulario(texto):
            if not _clave_permitida(termino, excluir):
                continue
            self.formas.setdefault(termino, Counter())[forma] += 1
            if termino not in vistos:
                vistos.add(termino)
                self.docs[termino] += 1
                if dias_doc:
                    self.dias.setdefault(termino, set()).update(dias_doc)

    def califica(self, palabra: str) -> bool:
        return (self.docs[palabra] >= self.min_docs
                and len(self.dias.get(palabra, ())) >= self.min_dias)


def _palabras_clave(corpus: Sequence[_Corpus], excluir: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    candidatas: set[str] = set()
    for c in corpus:
        candidatas.update(c.docs)
    salida: list[tuple[int, str, list[str]]] = []
    for palabra in candidatas:
        if not _clave_permitida(palabra, excluir):
            continue                         # segunda pasada: nombres de las cabeceras y sensibles
        fuentes = [c.nombre for c in corpus if c.docs[palabra]]
        docs = sum(c.docs[palabra] for c in corpus)
        if not (any(c.califica(palabra) for c in corpus)
                or (docs >= MIN_DOCS_MULTIFUENTE and len(fuentes) >= 2)):
            continue
        formas: Counter[str] = Counter()
        for c in corpus:
            formas.update(c.formas.get(palabra, {}))
        exactas = [(n, f) for f, n in formas.items() if _sin_tildes(f) == palabra]
        forma = max(exactas)[1] if exactas else palabra
        salida.append((docs, forma, fuentes))
    salida.sort(key=lambda t: (-t[0], t[1]))
    return [{"palabra": p, "freq": d, "fuentes": f} for d, p, f in salida[:MAX_PALABRAS]]


# ---------------------------------------------------------------------------
# Ensamblado del perfil
# ---------------------------------------------------------------------------

_PESOS_FUENTE: Final[Mapping[str, float]] = {"chrome": 0.4, "dominios": 0.3, "favoritos": 0.1, "drive": 0.15,
                                             "gemini": 0.05}


def _intereses(corpus: Sequence[_Corpus], visitas_categoria: Counter[str], visitas_total: int,
               incluir_sensibles: bool) -> list[dict[str, Any]]:
    puntos: Counter[str] = Counter()
    evidencia: Counter[str] = Counter()
    fuentes: dict[str, list[str]] = {}
    for c in corpus:
        if not c.n_docs:
            continue
        peso = _PESOS_FUENTE.get(c.nombre, 0.1)
        for categoria, n in c.cat_docs.items():
            puntos[categoria] += peso * n / c.n_docs
            evidencia[categoria] += n
            fuentes.setdefault(categoria, []).append(c.nombre)
    if visitas_total:
        for categoria, n in visitas_categoria.items():
            puntos[categoria] += _PESOS_FUENTE["dominios"] * n / visitas_total
            evidencia[categoria] += n
            fuentes.setdefault(categoria, []).append("chrome")
    sensibles = set(_SENSIBLES)
    maximo = max(puntos.values(), default=0.0)
    salida: list[dict[str, Any]] = []
    for categoria, valor in sorted(puntos.items(), key=lambda par: (-par[1], par[0])):
        if categoria in sensibles and not incluir_sensibles:
            continue
        if maximo <= 0 or evidencia[categoria] < MIN_EVIDENCIA_CATEGORIA:
            continue
        peso = round(valor / maximo, 2)
        if peso < MIN_PESO_CATEGORIA:
            continue
        item: dict[str, Any] = {"tema": categoria, "peso": peso,
                                "fuentes": sorted(set(fuentes.get(categoria, [])))}
        if categoria in sensibles:
            item["sensible"] = True
        salida.append(item)
    return salida[:MAX_INTERESES]


def _plataformas(hist: _ResHistorial | None, incluir_sensibles: bool) -> tuple[list[dict[str, Any]], int]:
    """``(sitios nombrables más usados, cuántos sitios frecuentes quedan sin nombrar)``.

    Se ordenan por **días distintos** de uso (las redirecciones y recargas inflan las visitas) y
    nunca incluyen ``google.com`` (va desglosado en ``servicios_google``) ni dominios desconocidos.
    """
    if hist is None or not hist.visitas:
        return [], 0
    total = sum(hist.visitas.values())
    salida: list[dict[str, Any]] = []
    sin_nombrar = 0
    for dominio, visitas in hist.visitas.items():
        dias = len(hist.dias.get(dominio, ()))
        if visitas < MIN_VISITAS_DOMINIO or dias < MIN_DIAS_DOMINIO or dominio == "google.com":
            continue
        if not _dominio_nombrable(dominio, incluir_sensibles):
            sin_nombrar += 1
            continue
        salida.append({"dominio": dominio, "visitas": visitas, "dias": dias, "peso": round(visitas / total, 3)})
    salida.sort(key=lambda p: (-p["dias"], -p["visitas"], p["dominio"]))
    return salida[:MAX_PLATAFORMAS], sin_nombrar


def _servicios_correo(res: _ResCorreo, cuenta: str | None,
                      incluir_sensibles: bool) -> tuple[list[dict[str, Any]], int]:
    """``(servicios nombrables que más escriben, cuántos quedan sin nombrar)``."""
    salida = []
    sin_nombrar = 0
    for dominio, n in sorted(res.servicios.items(), key=lambda par: (-par[1], par[0])):
        remitentes = res.remitente_servicio.get(dominio, Counter())
        if cuenta is not None:
            n -= remitentes.get(cuenta, 0)   # la propia cuenta nunca es un "servicio"
        if n < MIN_MENSAJES_SERVICIO:
            continue
        if not _dominio_nombrable(dominio, incluir_sensibles):
            sin_nombrar += 1                 # p. ej. el boletín de una persona en su propio dominio
            continue
        if len(salida) >= MAX_SERVICIOS_CORREO:
            continue
        categorias = res.categoria_servicio.get(dominio, Counter())
        categoria = next((c for c, _ in categorias.most_common() if c != "otros"), "otros")
        salida.append({"dominio": dominio, "mensajes": n, "categoria": categoria})
    return salida, sin_nombrar


def _plantilla_perfil(zona: tzinfo, incluir_sensibles: bool) -> dict[str, Any]:
    nombre_zona = getattr(zona, "key", None) or str(zona)
    return {
        "version": VERSION_PERFIL_TAKEOUT,
        "tipo": TIPO_PERFIL,
        "generado": datetime.now(UTC).isoformat(timespec="seconds"),
        "zona_horaria": nombre_zona[:64],
        "incluye_sensibles": bool(incluir_sensibles),
        "fuente": {},
        "nombre_pila": None,
        "intereses": [],
        "palabras_clave": [],
        "plataformas": [],
        "sitios_sin_nombrar": 0,
        "servicios_google": [],
        "sitios_guardados": [],
        "herramientas": {"extensiones": [], "sin_nombre": 0, "activas": 0},
        "gemini": {"gems": [], "n": 0},
        "drive": {"archivos": 0, "tipos": {}, "gb": 0.0, "nombres_automaticos": 0, "palabras_clave": []},
        "fotos": {"eventos": 0},
        "correo": {"mensajes": 0, "enviados": 0, "estilo": {"suficiente": False, "mensajes": 0,
                                                            "confianza": "insuficiente"},
                   "servicios": [], "servicios_sin_nombrar": 0, "bandeja": {}},
        "ritmo": {"navegacion": {"eventos": 0}, "correo_enviado": {"eventos": 0}, "fotos": {"eventos": 0}},
    }


def construir_perfil_takeout(zips: Sequence[Path | str], *, incluir_sensibles: bool = False,
                             progreso: Progreso | None = None, zona: tzinfo | str | None = None,
                             excluir: Iterable[str] = (), exclusiones: Exclusiones | None = None) -> dict[str, Any]:
    """Construye el perfil agregado a partir de los zips de Google Takeout (esquema versionado).

    - ``zips``: rutas (los duplicados exactos se ignoran: misma ruta o mismo contenido).
    - ``incluir_sensibles``: incluye las categorías especiales (por defecto se filtran).
    - ``progreso``: función opcional que recibe mensajes cortos de avance (sin datos personales).
    - ``zona``: zona horaria del usuario (``tzinfo`` o nombre IANA; por defecto America/Bogota).
    - ``excluir``: temas, palabras o dominios que el usuario pide quitar ahora (:func:`quitar_temas`).
    - ``exclusiones``: las que pidió antes (``takeout_exclusiones.json``); las de ``excluir`` se le
      añaden (el llamador las guarda con :func:`guardar_exclusiones`).

    Cada fuente se procesa con tolerancia a fallos: si una se rompe queda ``estado: "error"`` y
    las demás siguen. Nunca hace red ni llama a un LLM.
    """
    inicio = time.monotonic()
    zona_tz = _resolver_zona(zona)
    avisar = _progreso_seguro(progreso)
    ctx = _Contexto(zona_tz, bool(incluir_sensibles), avisar)
    perfil = _plantilla_perfil(zona_tz, ctx.incluir_sensibles)
    estados: dict[str, dict[str, Any]] = {}
    resultados: dict[str, Any] = {}
    with ExitStack() as pila:
        indice = _indexar(list(zips), pila, avisar)
        nombre_pila: str | None = None
        for clave in _CLAVES_FUENTES:
            entradas = indice.fuentes.get(clave, [])
            if not entradas:
                estados[clave] = {"estado": "ausente", "elementos": 0}
                continue
            avisar(f"Procesando {_NOMBRES_FUENTE[clave]}…")
            try:
                if clave == "cuenta":
                    resultado: Any = _procesar_cuenta(entradas, ctx)
                    nombre_pila = resultado.get("nombre_pila")
                elif clave == "historial":
                    resultado = _procesar_historial(entradas, ctx)
                elif clave == "favoritos":
                    resultado = _procesar_favoritos(entradas, ctx)
                elif clave == "extensiones":
                    resultado = _procesar_extensiones(entradas, ctx)
                elif clave == "gemini":
                    resultado = _procesar_gemini(entradas, ctx)
                elif clave == "drive":
                    resultado = _procesar_drive(entradas, ctx)
                elif clave == "fotos":
                    resultado = _procesar_fotos(entradas, ctx)
                else:
                    resultado = _procesar_correo(entradas, ctx)
            except Exception as exc:  # noqa: BLE001 - una fuente rota no tumba a las demás
                log.warning("La fuente %s falló (%s); sigo con las demás", clave, type(exc).__name__)
                estados[clave] = {"estado": "error", "motivo": type(exc).__name__[:40], "elementos": 0}
                continue
            resultados[clave] = resultado
            estados[clave] = {"estado": "ok", "elementos": _elementos(clave, resultado)}
    nombres = _ensamblar(perfil, resultados, ctx, nombre_pila)
    perfil["fuente"] = {
        "zips": indice.zips,
        "zips_duplicados": indice.zips_duplicados,
        "zips_ilegibles": indice.zips_ilegibles,
        "partes_faltantes": indice.partes_faltantes,
        "archivos": indice.entradas,
        "archivos_duplicados": indice.entradas_duplicadas,
        "excluidos_por_diseno": indice.excluidos,
        "filtrados_sensibles": ctx.filtrados_sensibles,
        "fuentes": estados,
        "segundos": round(time.monotonic() - inicio, 1),
    }
    if exclusiones is not None:
        _aplicar_exclusiones(perfil, exclusiones)
    terminos = [t for t in excluir if isinstance(t, str) and t.strip()]
    if terminos:
        quitar_temas(perfil, terminos, exclusiones)
    _verificar_privacidad(perfil, nombre_pila, nombres)
    nombres.clear()                          # los nombres de las cabeceras nunca salen de aquí
    log.info("Perfil de Takeout construido: %d fuentes con datos, %d intereses",
             sum(1 for e in estados.values() if e.get("estado") == "ok"), len(perfil["intereses"]))
    return perfil


def _elementos(clave: str, resultado: Any) -> int:
    if isinstance(resultado, dict):
        return int(resultado.get("elementos", 0) or 0)
    for atributo in ("entradas", "elementos", "archivos", "sidecars", "mensajes"):
        valor = getattr(resultado, atributo, None)
        if isinstance(valor, int):
            return valor
    return 0


def _progreso_seguro(progreso: Progreso | None) -> Progreso:
    def avisar(mensaje: str) -> None:
        log.debug("%s", mensaje)
        if progreso is None:
            return
        try:
            progreso(mensaje)
        except Exception as exc:  # un callback roto nunca tumba la ingesta
            log.debug("El callback de progreso falló (%s)", type(exc).__name__)
    return avisar


def _ensamblar(perfil: dict[str, Any], resultados: Mapping[str, Any], ctx: _Contexto,
               nombre_pila: str | None) -> set[str]:
    """Pasa los acumuladores (en memoria) a la forma agregada del perfil.

    Devuelve los tokens de nombres de personas (el nombre de pila y los de las cabeceras del
    correo) para la verificación final; el llamador los descarta después. Nunca se guardan.
    """
    perfil["nombre_pila"] = nombre_pila
    hist: _ResHistorial | None = resultados.get("historial")
    fav: _ResFavoritos | None = resultados.get("favoritos")
    drive: _ResDrive | None = resultados.get("drive")
    fotos: _ResFotos | None = resultados.get("fotos")
    correo: _ResCorreo | None = resultados.get("correo")
    gemini: dict[str, Any] | None = resultados.get("gemini")

    # Tokens que nunca son palabras clave, extensiones ni Gems: el nombre de pila y los nombres y
    # direcciones de las personas de las cabeceras del correo (incluido el apellido del usuario).
    nombres: set[str] = _tokens_nombre(nombre_pila or "")
    if correo is not None:
        nombres |= correo.nombres
        correo.nombres.clear()
    excluir_f = frozenset(nombres)

    corpus: list[_Corpus] = []
    if hist is not None:
        c_chrome = _Corpus("chrome", min_docs=MIN_DOCS_TITULOS, min_dias=MIN_DIAS_TITULOS)
        for limpio, modo, dias, _ in hist.titulos.values():
            c_chrome.agregar(limpio, modo=modo, dias=dias, excluir=excluir_f,
                             incluir_sensibles=ctx.incluir_sensibles)
        corpus.append(c_chrome)
        ctx.filtrados_sensibles += hist.filtrados
        hist.titulos.clear()
    if fav is not None:
        c_fav = _Corpus("favoritos", min_docs=MIN_DOCS_FAVORITOS)
        for texto, modo in fav.docs:
            c_fav.agregar(texto, modo=modo, excluir=excluir_f, incluir_sensibles=ctx.incluir_sensibles)
        corpus.append(c_fav)
        ctx.filtrados_sensibles += fav.filtrados
    c_drive: _Corpus | None = None
    if drive is not None:
        c_drive = _Corpus("drive", min_docs=MIN_DOCS_DRIVE)
        for texto in drive.docs:
            c_drive.agregar(texto, excluir=excluir_f, incluir_sensibles=ctx.incluir_sensibles)
        corpus.append(c_drive)
    gems: list[str] = []
    if gemini is not None:
        gems = [g for g in gemini.get("gems", []) if not (_tokens_nombre(g) & (excluir_f | _NOMBRES_PROPIOS_COMUNES))]
        c_gem = _Corpus("gemini", min_docs=10**6)    # solo suma a otras fuentes y a las categorías
        for nombre in gems:
            c_gem.agregar(nombre, excluir=excluir_f, incluir_sensibles=ctx.incluir_sensibles)
        corpus.append(c_gem)
    ctx.filtrados_sensibles += sum(c.filtrados for c in corpus)

    visitas_categoria: Counter[str] = Counter()
    visitas_total = 0
    if hist is not None:
        visitas_total = sum(hist.visitas.values())
        for dominio, n in hist.visitas.items():
            categoria = _DOMINIOS_INTERES.get(dominio)
            if categoria:
                visitas_categoria[categoria] += n
            sensible = _sensible_de_dominio(dominio)
            if sensible and ctx.incluir_sensibles:
                visitas_categoria[sensible] += n
    perfil["intereses"] = _intereses(corpus, visitas_categoria, visitas_total, ctx.incluir_sensibles)
    perfil["palabras_clave"] = _palabras_clave(corpus, excluir_f)

    if hist is not None:
        perfil["plataformas"], perfil["sitios_sin_nombrar"] = _plataformas(hist, ctx.incluir_sensibles)
        perfil["servicios_google"] = [{"servicio": s, "visitas": n}
                                      for s, n in hist.servicios_google.most_common(8) if n >= MIN_VISITAS_DOMINIO]
        perfil["ritmo"]["navegacion"] = hist.ritmo.a_dict()
    if fav is not None:
        perfil["sitios_guardados"] = [{"dominio": d, "favoritos": n}
                                      for d, n in sorted(fav.dominios.items(), key=lambda par: (-par[1], par[0]))
                                      if n >= MIN_FAVORITOS_DOMINIO and d != "google.com"
                                      and _dominio_nombrable(d, ctx.incluir_sensibles)][:MAX_SITIOS_GUARDADOS]
    extensiones = resultados.get("extensiones")
    if isinstance(extensiones, dict):
        nombres_ext = [e for e in extensiones.get("extensiones", [])
                       if not (_tokens_nombre(e) & (excluir_f | _NOMBRES_PROPIOS_COMUNES))]
        perfil["herramientas"] = {"extensiones": nombres_ext,
                                  "sin_nombre": int(extensiones.get("sin_nombre", 0))
                                  + len(extensiones.get("extensiones", [])) - len(nombres_ext),
                                  "activas": int(extensiones.get("activas", 0))}
    if gemini is not None:
        perfil["gemini"] = {"gems": gems[:MAX_GEMS], "n": len(gems)}
    if drive is not None:
        perfil["drive"] = {
            "archivos": drive.archivos,
            "tipos": {t: n for t, n in drive.tipos.most_common()},
            "gb": round(sum(drive.bytes_tipo.values()) / 1e9, 2),
            "nombres_automaticos": drive.automaticos,
            "palabras_clave": _palabras_clave([c_drive], excluir_f) if c_drive is not None else [],
        }
    if fotos is not None:
        ritmo_fotos = fotos.ritmo.a_dict(con_anios=True)
        perfil["fotos"] = {**ritmo_fotos, "videos": fotos.videos, "excluidas_papelera": fotos.excluidas,
                           "excluidas_ajenas": fotos.ajenas}
        perfil["ritmo"]["fotos"] = fotos.ritmo.a_dict()
    if correo is not None:
        cuenta = correo.entregado_a.most_common(1)[0][0] if correo.entregado_a else None
        if cuenta is None and correo.candidatos:
            cuenta = Counter(c.remitente for c in correo.candidatos).most_common(1)[0][0]
        enviados = [c for c in correo.candidatos if c.remitente == cuenta]
        ritmo_enviados = _Ritmo()
        for enviado in enviados:
            ritmo_enviados.agregar(enviado.momento)
        a_terceros = [c.texto for c in enviados
                      if c.texto and c.destinatarios and not c.destinatarios <= {cuenta}]
        recibidos = max(1, correo.recibidos)
        bandeja = {categoria: round(n / recibidos, 2) for categoria, n in correo.categorias.most_common()}
        bandeja["sin_leer"] = round(correo.sin_leer / recibidos, 2) if correo.recibidos else 0.0
        ctx.filtrados_sensibles += correo.filtrados
        servicios, servicios_sin_nombrar = _servicios_correo(correo, cuenta, ctx.incluir_sensibles)
        perfil["correo"] = {
            "mensajes": correo.mensajes - correo.excluidos,
            "excluidos_spam_papelera": correo.excluidos,
            "borradores_ignorados": correo.borradores,
            "enviados": len(enviados),
            "estilo": _estilo_correo(a_terceros, excluir_f),
            "servicios": servicios,
            "servicios_sin_nombrar": servicios_sin_nombrar,
            "bandeja": bandeja,
        }
        perfil["ritmo"]["correo_enviado"] = ritmo_enviados.a_dict()
        correo.candidatos.clear()
    return nombres


# ---------------------------------------------------------------------------
# Privacidad: verificación final y exclusiones pedidas por el usuario
# ---------------------------------------------------------------------------

_CLAVES_LIBRES: Final = frozenset({"generado", "zona_horaria", "tipo", "motivo", "estado"})
# Campos con texto que sale de los datos del usuario (no de un vocabulario fijo del programa): en
# ellos tampoco puede aparecer ningún nombre de las cabeceras del correo.
_CLAVES_CON_NOMBRES: Final = frozenset({"palabra", "ngrama", "dominio", "extensiones", "gems"})


def _verificar_privacidad(perfil: dict[str, Any], nombre_pila: str | None,
                          nombres: Iterable[str] = ()) -> None:
    """Segunda barrera: ninguna cadena del perfil (salvo ``nombre_pila``) puede llevar URLs,
    correos, números largos (también con separadores: ``300-123-4567``) ni el nombre del usuario;
    las etiquetas sacadas de sus datos (palabras, dominios, extensiones, Gems, muletillas) tampoco
    un nombre de las cabeceras del correo, y un dominio tiene que ser nombrable. Lo que falle se
    elimina."""
    tokens_nombre = _tokens_nombre(_norm(nombre_pila or ""))
    personas = frozenset(nombres) | tokens_nombre
    incluye_sensibles = perfil.get("incluye_sensibles") is True
    quitadas = 0

    def limpia(valor: Any, clave: str | None) -> Any:
        nonlocal quitadas
        if isinstance(valor, dict):
            return {k: limpia(v, k) for k, v in valor.items()}
        if isinstance(valor, list):
            salida = []
            for v in valor:
                limpio = limpia(v, clave)
                if limpio is None or limpio == "" or (isinstance(limpio, dict) and _vacio(limpio)):
                    quitadas += 1
                    continue
                salida.append(limpio)
            return salida
        if isinstance(valor, str) and clave not in _CLAVES_LIBRES:
            texto = valor
            tokens = _tokens_nombre(_norm(texto))
            if (_RE_URL_O_CORREO.search(texto) or _numero_largo(texto) or len(texto) > 80
                    or (tokens_nombre and tokens_nombre & tokens)):
                return ""
            if clave in _CLAVES_CON_NOMBRES and personas:
                partes = tokens | set(re.split(r"[.\-]", texto.lower())) if clave == "dominio" else tokens
                if partes & personas:
                    return ""
            if clave == "dominio" and (not _RE_DOMINIO_VALIDO.fullmatch(texto)
                                       or not _dominio_nombrable(texto, incluye_sensibles)):
                return ""
        return valor

    def _vacio(item: Mapping[str, Any]) -> bool:
        etiquetas = [item.get(k) for k in ("tema", "palabra", "dominio", "servicio", "ngrama") if k in item]
        return bool(etiquetas) and all(not e for e in etiquetas)

    pila_nombre = perfil.get("nombre_pila")
    for clave in list(perfil):
        if clave == "nombre_pila":
            continue
        perfil[clave] = limpia(perfil[clave], clave)
    perfil["nombre_pila"] = pila_nombre
    if quitadas:
        log.info("Verificación de privacidad: %d elementos descartados", quitadas)


class Exclusiones:
    """Temas, palabras o dominios que el usuario pidió quitar (``--quitar``).

    Se guardan en ``takeout_exclusiones.json`` (junto al perfil, 0o600) como HMAC-SHA256 con una
    clave local aleatoria, **nunca en claro**: el pedido puede ser sensible. Es ofuscación, no
    cifrado: quien tenga el archivo entero (clave incluida) puede probar palabras. Protege de un
    vistazo al perfil o a un log, y de tablas precalculadas. ``legado`` son los hashes sin clave
    (``sha256[:16]``) de un perfil v1, que se siguen respetando.
    """

    def __init__(self, clave: bytes | None = None, hashes: Iterable[str] = (), legado: Iterable[str] = ()) -> None:
        self.clave = clave if isinstance(clave, bytes) and len(clave) >= 16 else secrets.token_bytes(32)
        self.hashes: set[str] = {h for h in hashes if isinstance(h, str)}
        self.legado: set[str] = {h for h in legado if isinstance(h, str)}

    def __len__(self) -> int:
        return len(self.hashes) + len(self.legado)

    def _hmac(self, texto: str) -> str:
        return hmac.new(self.clave, texto.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:32]

    def agregar(self, texto: str) -> None:
        """Añade un término (se normaliza: minúsculas y sin tildes)."""
        norm = _norm(texto)
        if norm:
            self.hashes.add(self._hmac(norm))

    def coincide(self, etiqueta: str) -> bool:
        """¿La etiqueta (completa, una de sus palabras o una parte de un dominio) está excluida?"""
        claves = _claves_exclusion(etiqueta)
        if any(self._hmac(c) in self.hashes for c in claves):
            return True
        return bool(self.legado) and any(_hash_corto(c) in self.legado for c in claves)

    def a_dict(self) -> dict[str, Any]:
        return {"version": 1, "tipo": TIPO_EXCLUSIONES, "clave": self.clave.hex(),
                "hashes": sorted(self.hashes), "legado": sorted(self.legado)}


def _claves_exclusion(etiqueta: str) -> set[str]:
    """Textos (normalizados) con los que un elemento coincide con una exclusión: completo, sus
    palabras y las partes de un dominio."""
    norm = _norm(etiqueta)
    claves = {norm}
    claves |= {p for p in re.split(r"[^a-z0-9]+", norm) if len(p) >= 3 and p not in _TLD_GENERICOS}
    return {c for c in claves if c}


def _plano(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _norm(texto))


def _filtrar_perfil(perfil: dict[str, Any], quitar: Callable[[str, bool], bool],
                    quitados: list[str] | None = None) -> int:
    """Quita del perfil cada etiqueta para la que ``quitar(etiqueta, es_dominio)`` es cierto."""
    total = 0

    def filtrar(lista: Any, clave: str | None, es_dominio: bool = False) -> Any:
        nonlocal total
        if not isinstance(lista, list):
            return lista
        salida = []
        for item in lista:
            etiqueta = item.get(clave) if (clave and isinstance(item, Mapping)) else item
            if isinstance(etiqueta, str) and quitar(etiqueta, es_dominio):
                total += 1
                if quitados is not None and etiqueta not in quitados:
                    quitados.append(etiqueta)
                continue
            salida.append(item)
        return salida

    for clave, campo in (("intereses", "tema"), ("palabras_clave", "palabra"), ("servicios_google", "servicio")):
        perfil[clave] = filtrar(perfil.get(clave), campo)
    for clave in ("plataformas", "sitios_guardados"):
        perfil[clave] = filtrar(perfil.get(clave), "dominio", True)
    herramientas = perfil.get("herramientas")
    if isinstance(herramientas, dict):
        herramientas["extensiones"] = filtrar(herramientas.get("extensiones"), None)
    gemini = perfil.get("gemini")
    if isinstance(gemini, dict):
        gemini["gems"] = filtrar(gemini.get("gems"), None)
    drive = perfil.get("drive")
    if isinstance(drive, dict):
        drive["palabras_clave"] = filtrar(drive.get("palabras_clave"), "palabra")
    correo = perfil.get("correo")
    if isinstance(correo, dict):
        correo["servicios"] = filtrar(correo.get("servicios"), "dominio", True)
    return total


def _aplicar_exclusiones(perfil: dict[str, Any], exclusiones: Exclusiones) -> int:
    if not len(exclusiones):
        return 0
    return _filtrar_perfil(perfil, lambda etiqueta, _: exclusiones.coincide(etiqueta))


def quitar_temas(perfil: dict[str, Any], terminos: Iterable[str], exclusiones: Exclusiones | None = None, *,
                 quitados: list[str] | None = None) -> int:
    """Quita del perfil los temas, palabras, dominios, extensiones o Gems que el usuario pidió.

    Coinciden por el texto completo o por cualquiera de sus palabras (``"anime"`` quita la
    categoría «anime, manga y VTubers»); en un dominio, también si el término va pegado dentro
    (``"yolima"`` quita ``yolimacardozo.com``). Si se pasa ``exclusiones``, los términos (y los
    dominios quitados así, completos) se le añaden como HMAC para respetarlos al regenerar; el
    llamador las guarda (:func:`guardar_exclusiones`). ``quitados`` recibe las etiquetas quitadas
    (para enseñárselas al usuario). Devuelve cuántos elementos quitó.
    """
    normalizados = [n for n in (_norm(t) for t in terminos if isinstance(t, str)) if n]
    almacen = exclusiones if exclusiones is not None else Exclusiones()
    for termino in normalizados:
        almacen.agregar(termino)
    planos = [p for p in (_plano(t) for t in normalizados) if len(p) >= 4]

    def quitar(etiqueta: str, es_dominio: bool) -> bool:
        if almacen.coincide(etiqueta):
            return True
        if es_dominio and any(p in _plano(etiqueta.split(".", 1)[0]) for p in planos):
            almacen.agregar(etiqueta)        # el dominio completo: también al regenerar
            return True
        return False

    return _filtrar_perfil(perfil, quitar, quitados)


def ruta_exclusiones(ruta_perfil: Path | str) -> Path:
    """``takeout_exclusiones.json`` junto al perfil (olvidar el perfil no lo borra)."""
    return Path(ruta_perfil).with_name(NOMBRE_EXCLUSIONES)


def cargar_exclusiones(ruta: Path | str) -> Exclusiones:
    """Lee las exclusiones; ausentes o ilegibles ⇒ un almacén vacío con clave nueva (nunca lanza)."""
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return Exclusiones()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.warning("No se pudieron leer las exclusiones del perfil de Takeout: %s", type(exc).__name__)
        return Exclusiones()
    if not isinstance(datos, dict) or datos.get("tipo") != TIPO_EXCLUSIONES:
        log.warning("El archivo de exclusiones del perfil de Takeout no es válido; se ignora")
        return Exclusiones()
    try:
        clave = bytes.fromhex(str(datos.get("clave") or ""))
    except ValueError:
        clave = b""
    hashes = datos.get("hashes") if isinstance(datos.get("hashes"), list) else []
    legado = datos.get("legado") if isinstance(datos.get("legado"), list) else []
    if hashes and len(clave) < 16:
        log.warning("Las exclusiones del perfil de Takeout no tienen una clave válida; se descartan")
        hashes = []
    return Exclusiones(clave, hashes, legado)


def guardar_exclusiones(exclusiones: Exclusiones, ruta: Path | str) -> None:
    """Escribe las exclusiones (atómico, 0o600). Lanza ``OSError`` si no se puede."""
    _escribir_json_privado(exclusiones.a_dict(), Path(ruta))


def _migrar_exclusiones(perfil: dict[str, Any], exclusiones: Exclusiones) -> bool:
    """Pasa las exclusiones de un perfil v1 (``perfil["exclusiones"]``) al almacén aparte."""
    viejas = perfil.pop("exclusiones", None)
    if not isinstance(viejas, list):
        return False
    nuevas = {h for h in viejas if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{16}", h)} - exclusiones.legado
    exclusiones.legado |= nuevas
    return bool(nuevas)


def perfil_vigente(perfil: Mapping[str, Any]) -> bool:
    """¿Se puede usar el perfil (prompt y memoria)? Solo desde ``VERSION_MINIMA_EN_USO``: los v1
    se generaron con filtros de privacidad más débiles y hay que regenerarlos."""
    version = perfil.get("version") if isinstance(perfil, Mapping) else None
    return isinstance(version, int) and not isinstance(version, bool) and version >= VERSION_MINIMA_EN_USO


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def guardar_perfil(perfil: Mapping[str, Any], ruta: Path | str) -> None:
    """Escribe el perfil en JSON (UTF-8) de forma atómica (``.tmp`` + ``os.replace``) con 0o600.

    Lanza ``OSError`` si no se puede escribir (la CLI lo convierte en el código 1).
    """
    _escribir_json_privado(perfil, Path(ruta))
    log.info("Perfil de Takeout guardado")


def _escribir_json_privado(datos_json: Mapping[str, Any], ruta: Path) -> None:
    """JSON (UTF-8, LF) atómico y privado: ``.tmp`` con 0o600 + ``os.replace``."""
    try:
        ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("No se pudo crear la carpeta del perfil de Takeout: %s", type(exc).__name__)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    datos = (json.dumps(dict(datos_json), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    banderas = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporal, banderas, 0o600)
    try:
        try:
            archivo = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with archivo:
            archivo.write(datos)
            archivo.flush()
            os.fsync(archivo.fileno())
        _chmod_privado(temporal)
        os.replace(temporal, ruta)
    except BaseException:
        try:
            temporal.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("No se pudo borrar el temporal del perfil de Takeout: %s", type(exc).__name__)
        raise
    _chmod_privado(ruta)
    log.debug("Archivo privado del perfil de Takeout escrito (%d bytes)", len(datos))


guardar_perfil_takeout = guardar_perfil


def cargar_perfil_takeout(ruta: Path | str) -> dict[str, Any] | None:
    """Lee ``perfil_takeout.json``. Ausente, ilegible o inválido ⇒ ``None`` (nunca lanza)."""
    try:
        texto = Path(ruta).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        log.debug("No hay perfil de Takeout todavía")
        return None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.warning("No se pudo leer el perfil de Takeout: %s", type(exc).__name__)
        return None
    try:
        datos = json.loads(texto)
    except ValueError:
        log.warning("El perfil de Takeout no es JSON válido; se ignora")
        return None
    if not isinstance(datos, dict):
        log.warning("El perfil de Takeout no es un objeto JSON; se ignora")
        return None
    if datos.get("tipo", TIPO_PERFIL) != TIPO_PERFIL:
        log.warning("El archivo no es un perfil de Takeout; se ignora")
        return None
    version = datos.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > VERSION_PERFIL_TAKEOUT:
        log.warning("Perfil de Takeout v%d más nuevo que este programa (v%d); se usa lo que se entienda",
                    version, VERSION_PERFIL_TAKEOUT)
    return datos


# ---------------------------------------------------------------------------
# Resumen para el prompt
# ---------------------------------------------------------------------------


def _items(valores: Any, clave: str | None, maximo: int, *, largo: int = 40) -> list[str]:
    if not isinstance(valores, (list, tuple)):
        return []
    salida: list[str] = []
    for valor in valores:
        crudo = valor.get(clave) if (clave and isinstance(valor, Mapping)) else valor
        item = _item(crudo, largo)
        if item and item not in salida:
            salida.append(item)
        if len(salida) >= maximo:
            break
    return salida


def _ritmo_valido(ritmo: Any) -> Mapping[str, Any] | None:
    if not isinstance(ritmo, Mapping):
        return None
    eventos = _real(ritmo.get("eventos"))
    hora = ritmo.get("hora_pico")
    if eventos is None or eventos < MIN_EVENTOS_RITMO:
        return None
    if not isinstance(hora, int) or isinstance(hora, bool) or not 0 <= hora <= 23:
        return None
    return ritmo


def _dia(ritmo: Mapping[str, Any], *, plural: bool) -> str:
    dia = ritmo.get("dia_pico")
    if dia not in _DIAS:
        return ""
    return _DIAS_PLURAL[_DIAS.index(dia)] if plural else dia


def _frase_correo(correo: Mapping[str, Any]) -> str:
    estilo = correo.get("estilo") if isinstance(correo.get("estilo"), Mapping) else {}
    if not estilo.get("suficiente"):
        return ""
    longitud = _real(estilo.get("longitud_mediana_palabras")) or _real(estilo.get("longitud_media_palabras"))
    if not longitud or not 0 < longitud <= 10_000:
        return ""
    tamano = "breves" if longitud <= 40 else "de longitud media" if longitud <= 120 else "largos"
    palabras = max(1, round(longitud))
    partes = [f"Sus correos son {tamano} (~{palabras} {'palabra' if palabras == 1 else 'palabras'})"]
    formalidad = estilo.get("formalidad")
    tratamiento = estilo.get("tratamiento")
    tono = {"formal": "tono formal", "informal": "tono cercano", "neutra": "tono neutro"}.get(str(formalidad), "")
    trato = {"usted": "trata de usted", "tú": "tutea", "mixto": "alterna tú y usted"}.get(str(tratamiento), "")
    if tono or trato:
        partes.append(", ".join(p for p in (tono, trato) if p))
    saludos = _items(estilo.get("saludos"), None, 2)
    despedidas = _items(estilo.get("despedidas"), None, 2)
    if saludos:
        partes.append("saluda con " + " o ".join(f"«{s}»" for s in saludos))
    if despedidas:
        partes.append("se despide con " + " o ".join(f"«{d}»" for d in despedidas))
    frase = "; ".join(partes)
    if estilo.get("confianza") == "baja":
        frase += " (pocos correos: confianza baja)"
    return frase + "."


def resumen_perfil_takeout(perfil: Mapping[str, Any], max_chars: int = MAX_RESUMEN) -> str:
    """Resumen en español natural (≤ ``max_chars``, 900 por defecto) para el prompt de Broca.

    Nunca incluye el nombre de pila, direcciones, URLs, números largos ni títulos literales: solo
    categorías, palabras agregadas, dominios, horarios y estilo. Ejemplo: ``"Por su actividad en
    Google le interesan la programación y desarrollo, la inteligencia artificial y la música.
    Sitios que más usa: youtube.com, github.com. Navega sobre todo hacia las 22 h (noche)…"``.
    """
    if not isinstance(perfil, Mapping):
        return ""
    limite = max(1, int(max_chars))
    frases: list[str] = []

    intereses = _items(perfil.get("intereses"), "tema", 6)
    if intereses:
        frases.append(f"Por su actividad en Google le interesan: {_lista_y(intereses)}.")
    palabras = _items(perfil.get("palabras_clave"), "palabra", 8)
    if palabras:
        frases.append("Palabras que se repiten en lo que lee y guarda: " + ", ".join(palabras) + ".")
    plataformas = _items(perfil.get("plataformas"), "dominio", 6)
    if plataformas:
        frases.append("Sitios que más usa: " + ", ".join(plataformas) + ".")
    google = _items(perfil.get("servicios_google"), "servicio", 4)
    if google:
        frases.append(f"De Google usa sobre todo {_lista_y(google)}.")
    ritmo = perfil.get("ritmo") if isinstance(perfil.get("ritmo"), Mapping) else {}
    navegacion = _ritmo_valido(ritmo.get("navegacion"))
    if navegacion:
        franja = _FRANJA_TEXTO.get(str(navegacion.get("franja")), _FRANJA_TEXTO[_franja(navegacion["hora_pico"])])
        frase = f"Navega sobre todo hacia las {navegacion['hora_pico']} h ({franja})"
        dia = _dia(navegacion, plural=False)
        frases.append(frase + (f"; su día más activo es el {dia}." if dia else "."))
    correo = perfil.get("correo") if isinstance(perfil.get("correo"), Mapping) else {}
    frase_correo = _frase_correo(correo)
    if frase_correo:
        frases.append(frase_correo)
    enviado = _ritmo_valido(ritmo.get("correo_enviado"))
    if enviado:
        frases.append(f"Suele escribir correos hacia las {enviado['hora_pico']} h.")
    servicios = _items(correo.get("servicios"), "dominio", 4)
    if servicios:
        categorias = [c for c in _items(correo.get("servicios"), "categoria", 6) if c != "otros"]
        detalle = f" ({_lista_y(list(dict.fromkeys(categorias))[:3])})" if categorias else ""
        frases.append(f"Le escriben sobre todo servicios{detalle}: " + ", ".join(servicios) + ".")
    herramientas = perfil.get("herramientas") if isinstance(perfil.get("herramientas"), Mapping) else {}
    extensiones = _items(herramientas.get("extensiones"), None, 4)
    if extensiones:
        frases.append(f"En el navegador usa extensiones como {_lista_y(extensiones)}.")
    gemini = perfil.get("gemini") if isinstance(perfil.get("gemini"), Mapping) else {}
    gems = _items(gemini.get("gems"), None, 4)
    if gems:
        frases.append("Tiene Gems de Gemini como " + ", ".join(f"«{g}»" for g in gems) + ".")
    fotos = perfil.get("fotos") if isinstance(perfil.get("fotos"), Mapping) else {}
    if _ritmo_valido(fotos):
        dia = _dia(fotos, plural=True)
        anio = fotos.get("anio_pico")
        frase = f"Toma más fotos {'los ' + dia + ' ' if dia else ''}hacia las {fotos['hora_pico']} h"
        if isinstance(anio, int) and not isinstance(anio, bool) and 1995 <= anio <= 2100:
            frase += f"; el año con más fotos fue {anio}"
        frases.append(frase + ".")
    drive = perfil.get("drive") if isinstance(perfil.get("drive"), Mapping) else {}
    tipos = drive.get("tipos") if isinstance(drive.get("tipos"), Mapping) else {}
    tipos_top = [_TIPOS_PLURAL.get(t, "") for t, _ in sorted(
        ((t, n) for t, n in tipos.items() if isinstance(n, int) and n > 0 and t != "otro"),
        key=lambda par: -par[1])[:2]]
    tipos_top = [t for t in tipos_top if t]
    palabras_drive = _items(drive.get("palabras_clave"), "palabra", 4)
    if tipos_top or palabras_drive:
        frase = "En Drive guarda sobre todo " + _lista_y(tipos_top) if tipos_top else "En Drive"
        if palabras_drive:
            frase += ("; " if tipos_top else " ") + "temas en los nombres: " + ", ".join(palabras_drive)
        frases.append(frase + ".")

    if not frases:
        return "Perfil de Google con pocos datos."
    resumen = ""
    for frase in frases:
        candidato = f"{resumen} {frase}".strip()
        if len(candidato) > limite:
            continue
        resumen = candidato
    if not resumen:
        resumen = frases[0][: max(0, limite - 1)].rstrip() + "…" if limite > 1 else frases[0][:limite]
    return resumen[:limite]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _settings_cli() -> Any:
    """``cargar_settings()`` (``core.config`` se importa solo aquí); ``None`` si no está."""
    try:
        from core.config import cargar_settings
    except ImportError as exc:  # ejecución fuera del proyecto
        log.debug("core.config no disponible (%s)", exc)
        return None
    return cargar_settings()


def _ruta_defecto(settings: Any) -> Path:
    ruta = getattr(settings, "perfil_takeout_path", None) if settings is not None else None
    if isinstance(ruta, Path):
        return ruta
    return Path(__file__).resolve().parent.parent / "data" / "john" / "perfil_takeout.json"


def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.takeout_ingestor",
        description="Convierte tu Google Takeout (zips) en perfil_takeout.json: solo patrones agregados "
                    "tuyos; todo se lee dentro de los zips y nada sale de tu equipo.")
    parser.add_argument("zips", nargs="*", metavar="ZIP", help="partes del Takeout (.zip); los duplicados se ignoran")
    parser.add_argument("--salida", metavar="RUTA", help="ruta del perfil (por defecto, la de Settings)")
    parser.add_argument("--incluir-sensibles", action="store_true",
                        help="incluye salud, sexualidad, religión, política, finanzas y legal (por defecto se filtran)")
    parser.add_argument("--mostrar", action="store_true",
                        help="muestra el detalle agregado (intereses, palabras, sitios…); sin zips, el del perfil guardado")
    parser.add_argument("--quitar", metavar="T1,T2", default="",
                        help="temas, palabras o dominios que nunca deben salir (se guardan ofuscados, aparte)")
    parser.add_argument("--zona", metavar="TZ", help="zona horaria IANA (por defecto, la de Settings)")
    parser.add_argument("--reemplazar", action="store_true",
                        help="sobrescribe el perfil guardado aunque el nuevo traiga menos fuentes")
    return parser


def _detalle(perfil: Mapping[str, Any]) -> list[str]:
    """Detalle agregado para revisar el perfil (sin el nombre de pila)."""
    lineas = ["Detalle agregado del perfil:"]
    for titulo, valores, clave in (
            ("Intereses", perfil.get("intereses"), "tema"),
            ("Palabras clave", perfil.get("palabras_clave"), "palabra"),
            ("Sitios", perfil.get("plataformas"), "dominio"),
            ("Servicios de Google", perfil.get("servicios_google"), "servicio"),
            ("Sitios guardados", perfil.get("sitios_guardados"), "dominio"),
            ("Le escriben", (perfil.get("correo") or {}).get("servicios") if isinstance(perfil.get("correo"), Mapping)
             else None, "dominio"),
            ("Extensiones", (perfil.get("herramientas") or {}).get("extensiones")
             if isinstance(perfil.get("herramientas"), Mapping) else None, None),
            ("Gems", (perfil.get("gemini") or {}).get("gems") if isinstance(perfil.get("gemini"), Mapping) else None,
             None),
            ("Palabras de Drive", (perfil.get("drive") or {}).get("palabras_clave")
             if isinstance(perfil.get("drive"), Mapping) else None, "palabra")):
        items = _items(valores, clave, 30)
        if items:
            lineas.append(f"  {titulo}: " + ", ".join(items))
    return lineas


def _estadisticas(perfil: Mapping[str, Any]) -> list[str]:
    fuente = perfil.get("fuente") if isinstance(perfil.get("fuente"), Mapping) else {}
    lineas = [f"Zips procesados: {fuente.get('zips', 0)} · duplicados ignorados: {fuente.get('zips_duplicados', 0)}"
              f" · ilegibles: {fuente.get('zips_ilegibles', 0)}"]
    faltantes = fuente.get("partes_faltantes") or []
    if faltantes:
        lineas.append("Faltan partes del Takeout: " + ", ".join(str(f) for f in faltantes)
                      + " (el perfil se generó sin ellas).")
    lineas.append(f"Archivos en el índice: {fuente.get('archivos', 0)} · excluidos por diseño (sin abrir): "
                  f"{fuente.get('excluidos_por_diseno', 0)} · elementos de categorías sensibles filtrados: "
                  f"{fuente.get('filtrados_sensibles', 0)}")
    correo = perfil.get("correo") if isinstance(perfil.get("correo"), Mapping) else {}
    sin_nombrar = (perfil.get("sitios_sin_nombrar") or 0, correo.get("servicios_sin_nombrar") or 0)
    if any(isinstance(n, int) and n > 0 for n in sin_nombrar):
        lineas.append(f"Sitios frecuentes sin nombrar (no están en la lista de sitios conocidos): {sin_nombrar[0]}"
                      f" · servicios de correo sin nombrar: {sin_nombrar[1]}")
    estados = fuente.get("fuentes") if isinstance(fuente.get("fuentes"), Mapping) else {}
    for clave in _CLAVES_FUENTES:
        estado = estados.get(clave) if isinstance(estados.get(clave), Mapping) else None
        if estado is None:
            continue
        texto = str(estado.get("estado", "?"))
        if texto == "error":
            texto += f" ({estado.get('motivo', '?')})"
        lineas.append(f"  - {_NOMBRES_FUENTE[clave]}: {texto}, {estado.get('elementos', 0)} elementos")
    if isinstance(fuente.get("segundos"), (int, float)):
        lineas.append(f"Tiempo: {_decimal(float(fuente['segundos']))} s")
    return lineas


AVISO_FILTRO: Final = ("Aviso: el filtro de categorías sensibles (salud, sexualidad, religión, política, finanzas y "
                       "legal) es un léxico curado y no es exhaustivo. Revisa el resumen y quita lo que no quieras "
                       "con --quitar.")


def _hechos_cli(perfil: Mapping[str, Any], settings: Any) -> list[str]:
    """Los recuerdos heredados que main guardará en la memoria (y que pueden viajar al LLM como
    recuerdos relevantes con ``RAMS_PERFIL_TAKEOUT_EN_PROMPT=1``)."""
    try:
        from core.memory_engine import hechos_desde_takeout
    except ImportError as exc:  # ejecución fuera del proyecto
        log.debug("core.memory_engine no disponible (%s)", exc)
        return []
    trato = getattr(settings, "trato_usuario", None)
    return hechos_desde_takeout(perfil, trato if isinstance(trato, str) and trato.strip() else "Lab Mem 001")


def _fuentes_ok(perfil: Mapping[str, Any]) -> set[str]:
    fuente = perfil.get("fuente") if isinstance(perfil.get("fuente"), Mapping) else {}
    estados = fuente.get("fuentes") if isinstance(fuente.get("fuentes"), Mapping) else {}
    return {c for c, e in estados.items() if isinstance(e, Mapping) and e.get("estado") == "ok"}


def _guardar_todo(perfil: Mapping[str, Any] | None, ruta: Path, exclusiones: Exclusiones,
                  ruta_exc: Path, *, con_exclusiones: bool) -> bool:
    try:
        if con_exclusiones:
            guardar_exclusiones(exclusiones, ruta_exc)
        if perfil is not None:
            guardar_perfil(perfil, ruta)
    except OSError as exc:
        _imprimir(f"No se pudo guardar en {ruta.parent}: {exc.strerror or type(exc).__name__}", error=True)
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """CLI. Códigos: 0 OK · 1 error de lectura/escritura, sin datos o perfil más completo · 2 uso incorrecto."""
    parser = _construir_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as salida:  # --help (0) o uso incorrecto (2)
        return salida.code if isinstance(salida.code, int) else 2
    quitar = [t.strip() for t in str(args.quitar or "").split(",") if t.strip()]
    settings = None if (args.salida and (args.zona or not args.zips)) else _settings_cli()
    ruta = Path(args.salida).expanduser() if args.salida else _ruta_defecto(settings)
    ruta_exc = ruta_exclusiones(ruta)
    exclusiones = cargar_exclusiones(ruta_exc)

    if not args.zips:
        if not (args.mostrar or quitar):
            _imprimir("Indica los zips del Takeout (o --mostrar / --quitar sobre el perfil guardado).", error=True)
            return 2
        perfil = cargar_perfil_takeout(ruta)
        if perfil is None:
            _imprimir(f"No hay un perfil de Takeout legible en {ruta}.", error=True)
            return 1
        migradas = _migrar_exclusiones(perfil, exclusiones)
        if quitar or migradas:
            quitados: list[str] = []
            n = quitar_temas(perfil, quitar, exclusiones, quitados=quitados) if quitar else 0
            if not _guardar_todo(perfil, ruta, exclusiones, ruta_exc, con_exclusiones=True):
                return 1
            if quitar:
                detalle = (": " + ", ".join(quitados)) if quitados else ""
                _imprimir(f"Quitados {n} elementos del perfil{detalle}. Las exclusiones se guardan ofuscadas en "
                          f"{ruta_exc.name} y se respetan al regenerar.")
        if not perfil_vigente(perfil):
            _imprimir("Este perfil es de una versión anterior (filtros de privacidad más débiles): Amadeus no lo "
                      "usa. Regenéralo con los zips.", error=True)
        _imprimir("Resumen que verá Amadeus:")
        _imprimir(resumen_perfil_takeout(perfil))
        if args.mostrar:
            for linea in _detalle(perfil):
                _imprimir(linea)
        return 0

    zona: Any = args.zona or getattr(settings, "zona_horaria", None)

    def progreso(mensaje: str) -> None:
        _imprimir(mensaje, error=True)

    previo = cargar_perfil_takeout(ruta)
    migradas = previo is not None and _migrar_exclusiones(previo, exclusiones)
    antes = len(exclusiones)
    perfil = construir_perfil_takeout([Path(z).expanduser() for z in args.zips],
                                      incluir_sensibles=args.incluir_sensibles, progreso=progreso, zona=zona,
                                      excluir=quitar, exclusiones=exclusiones)
    if perfil["fuente"].get("zips", 0) == 0:
        _imprimir("Ningún zip del Takeout se pudo leer.", error=True)
        return 1
    nuevas = _fuentes_ok(perfil)
    if not nuevas:
        _imprimir("Ninguna fuente del Takeout se pudo leer (¿son los zips correctos?); no guardo nada.", error=True)
        return 1
    if previo is not None and not args.reemplazar:
        faltan = sorted(_fuentes_ok(previo) - nuevas, key=lambda c: _CLAVES_FUENTES.index(c)
                        if c in _CLAVES_FUENTES else 99)
        if faltan:
            nombres = ", ".join(_NOMBRES_FUENTE.get(c, c) for c in faltan)
            _imprimir(f"El perfil guardado tiene fuentes que estos zips no traen ({nombres}); no lo sobrescribo. "
                      "Pasa todas las partes del Takeout o usa --reemplazar.", error=True)
            return 1
    if not _guardar_todo(perfil, ruta, exclusiones, ruta_exc,
                         con_exclusiones=bool(migradas or len(exclusiones) != antes or quitar)):
        return 1
    _imprimir("Resumen que verá Amadeus (revísalo; puedes quitar temas con --quitar):")
    _imprimir(resumen_perfil_takeout(perfil))
    _imprimir("")
    _imprimir(AVISO_FILTRO)
    hechos = _hechos_cli(perfil, settings)
    if hechos:
        _imprimir("Recuerdos heredados que guardará en su memoria (con RAMS_PERFIL_TAKEOUT_EN_PROMPT=1 también "
                  "pueden ir al LLM como recuerdos relevantes):")
        for hecho in hechos:
            _imprimir(f"  - {hecho}")
    _imprimir("")
    for linea in _estadisticas(perfil):
        _imprimir(linea)
    if args.mostrar:
        for linea in _detalle(perfil):
            _imprimir(linea)
    _imprimir(f"Perfil guardado en {ruta}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
