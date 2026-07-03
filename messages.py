# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  WorgaNomoR
"""
Тексты и форматирование ShikiUpdatesBot.

Банк шаблонов уведомлений, парсеры описаний истории, построение сообщений
и презентационные форматтеры отчётов. Зависит от config/utils/shiki_api;
доменную агрегацию (stats) и хендлеры не знает — они зависят от него.
"""

import random
import re
from datetime import datetime, timezone

from config import (
    DISPLAY_NAME,
    SHIKI_BASE_URL,
)
from shiki_api import get_media_info
from utils import _fmt_dt_short, _human_ago, _rel_url, _safe_int, h

# ═══════════════════════════════════════════════════════════════════
#  БАНК СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════

MESSAGES = {

    # ────────────────────────────────
    #  АНИМЕ
    # ────────────────────────────────

    "anime": {

        # 📋 Добавил в «Запланированное»
        "planned": [
            "📋 {n} закинул <b>{title}</b> в бесконечный список «посмотрю когда-нибудь». Ждём.",
            "🗂️ <b>{title}</b> занял своё место в очереди. Дождётся ли? Обязательно! Скоро ли? Ну, как повезет!",
            "📌 <b>{title}</b> теперь в планах у {n}. Очередь живая, дойдёт черёд.",
            "🧠 {n} взял <b>{title}</b> на заметку. В список к просмотру попадает только отобранное.",
            "🔖 <b>{title}</b> добавлено в коллекцию намерений {n}. Осталось только посмотреть.",
            "📥 <b>{title}</b> отправляется в список к просмотру. Что-то в нём зацепило {n} 👀",
            "📥 {n} закинул <b>{title}</b> в список. Дойдут руки — а они дойдут.",
            "📌 <b>{title}</b> теперь в планах. {n} редко бросает список на полпути.",
            "🔖 {n} присмотрел <b>{title}</b>. Очередь движется, честно.",
            "👀 <b>{title}</b> в списке к просмотру. {n} уже прикидывает, когда втиснуть.",
            "🎯 {n} добавил <b>{title}</b> в планы. Не «когда-нибудь потом», а вполне себе скоро... или не очень.",
            "🍿 <b>{title}</b> ждёт своей очереди у {n}. И таки дождётся.",
            "🧠 {n} занёс <b>{title}</b> в список. Память подвести может — список нет.",
            "📋 Ещё один тайтл в планах у {n}. <b>{title}</b>, ты следующий. Ну, может через парочку.",
        ],

        # ▶️ Начал смотреть
        "watching": [
            "▶️ {n} начал смотреть <b>{title}</b>. Запасаемся попкорном.",
            "🎬 Поехали! <b>{title}</b> запущено. Возврата нет.",
            "👁️ {n} открыл <b>{title}</b> и пропал. Ждём отчёта.",
            "🍿 <b>{title}</b> в плеере, {n} у экрана. Классика.",
            "🚀 Старт! <b>{title}</b> вышло на орбиту просмотра.",
            "😤 {n} не выдержал и таки начал <b>{title}</b>. Посмотрим, чем это закончится.",
            "🎬 Поехали — <b>{title}</b> в плеере у {n}. Возврата нет.",
            "👀 {n} открыл <b>{title}</b> и пропал. Если что, он у экрана.",
            "🍿 <b>{title}</b> пошло. {n} устроился поудобнее.",
            "🚀 {n} взялся за <b>{title}</b>. Посмотрим, затянет или дропнет.",
            "😎 {n} наконец дошёл до <b>{title}</b>. Списку полегчало на один тайтл.",
            "🔥 <b>{title}</b> стартовало у {n}. Ставки на то, сколько серий за раз, принимаются.",
            "📺 {n} включил <b>{title}</b>. «Ещё одну серию и спать» — классика.",
            "⏯️ <b>{title}</b> в процессе у {n}. Дороги назад нет, только до финала.",
        ],

        # 🔁 Пересматривает
        "rewatching": [
            "🔁 {n} пересматривает <b>{title}</b>. Не надоело — значит шедевр (или мазохизм).",
            "♻️ <b>{title}</b> снова в деле. {n} возвращается к проверенному.",
            "🌀 Повторный заход на <b>{title}</b>. Уважаю.",
            "📺 {n} включил <b>{title}</b> ещё раз. Некоторые вещи просто не отпускают.",
            "🔂 <b>{title}</b> на втором (третьем? десятом?) круге у {n}. Это уже традиция.",
            "👏 Решился на ремастер впечатлений — <b>{title}</b> снова смотрит {n}.",
            "🔁 {n} пересматривает <b>{title}</b>. Значит, зацепило не на один раз.",
            "♻️ <b>{title}</b> снова в плеере у {n}. Хорошее не стареет.",
            "🌀 {n} вернулся к <b>{title}</b>. Некоторые вещи тянет пересмотреть.",
        ],

        # 💀 Бросил (dropped)
        "dropped": [
            "🗑️ <b>{title}</b> — в мусор. {n} не пощадил.",
            "💀 Dropped. <b>{title}</b> не пережило встречи с {n}.",
            "🚪 {n} покинул <b>{title}</b> без объяснений. Бывает.",
            "❌ <b>{title}</b> — дропнуто. Минус одно аниме в этом жестоком мире.",
            "😤 {n} посмотрел на <b>{title}</b> и сказал «нет». Твёрдая позиция.",
            "🏳️ <b>{title}</b> не справилось с испытанием {n}. Позор или избавление — решай сам.",
        ],

        # ✅ Завершил без оценки
        "completed_no_score": [
            "✅ {n} досмотрел <b>{title}</b>. Оценку зажал — интригует.",
            "🏁 <b>{title}</b> завершено. Впечатления {n} покрыты тайной.",
            "👀 Конец <b>{title}</b>. Молчание {n} красноречивее слов.",
            "📺 {n} прошёл путь <b>{title}</b> до конца. Без комментариев.",
            "🎌 <b>{title}</b> — пройдено. Оценка — не для слабонервных, видимо.",
            "🤐 Закончил <b>{title}</b> и молчит. Либо шедевр, либо травма.",
            "✅ {n} досмотрел <b>{title}</b>. Без оценки — иногда и так бывает.",
            "🏁 <b>{title}</b> завершено. {n} оценку не поставил, и это его право.",
            "📺 {n} закрыл <b>{title}</b>. Молча, но с чувством выполненного долга.",
            "🎬 <b>{title}</b> досмотрено. Оценка? Может, позже. Может, никогда.",
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 <b>{title}</b> — {score}/10. {n} страдал, но добил. Настоящий герой.",
            "😭 {score}/10 за <b>{title}</b>. Боль реальна. Зачем вообще?",
            "🤮 <b>{title}</b> получает {score}/10 от {n}. Это приговор.",
            "⚰️ {score}/10 — <b>{title}</b> мертво и похоронено в памяти {n}.",
            "🧟 {n} выжил после <b>{title}</b> ({score}/10). Это уже достижение.",
            "🔥 <b>{title}</b> — {score}/10. Сожжено дотла заслуженно.",
            "📉 <b>{title}</b> — {score}/10. {n} честно домучил. За что — вопрос открытый.",
            "🫠 {score}/10. <b>{title}</b> высосало время {n} и не извинилось.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 <b>{title}</b> — {score}/10. Ни рыба ни мясо, говорит {n}.",
            "🫤 {score}/10 за <b>{title}</b>. Не плохо, не хорошо. Просто... было.",
            "🤷 {n} поставил <b>{title}</b> {score}/10. Среднячок прожил и умер.",
            "📊 <b>{title}</b> — твёрдый {score}/10. {n} явно ожидал большего.",
            "🌫️ {score}/10 — <b>{title}</b> оставило {n} в тумане безразличия.",
            "😶 Посмотрел. Оценил. {score}/10. <b>{title}</b> не потрясло мир {n}.",
            "⚖️ <b>{title}</b> — {score}/10. {n} посмотрел. Бывает и так.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 <b>{title}</b> — {score}/10! {n} доволен. Хороший вкус подтверждён.",
            "🔥 {score}/10 за <b>{title}</b>! {n} в восторге, и это заслужено.",
            "👏 <b>{title}</b> получает {score}/10 от {n}. Браво, студия!",
            "✨ {score}/10 — <b>{title}</b> попало в сердечко {n}.",
            "🎉 Вот это да! {score}/10 за <b>{title}</b>. Рекомендую к просмотру всем.",
            "💫 <b>{title}</b> — {score}/10. {n} явно не разочарован. Редкий случай.",
            "📈 {score}/10 — <b>{title}</b> попало в {n}. Почти идеально, но десятка — это святое.",
            "🎯 <b>{title}</b> заработало {score}/10. {n} доволен и не скрывает.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 <b>{title}</b> — ДЕСЯТКА! {n} нашёл новый фаворит. Занесите в анналы.",
            "🏆 10/10! <b>{title}</b> вошло в пантеон {n}. Это серьёзно.",
            "💎 {n} раздаёт десятки! <b>{title}</b> — абсолютный шедевр по его версии.",
            "🌌 10/10 за <b>{title}</b>. {n} разрушен и счастлив одновременно.",
            "🎌 Максимум! <b>{title}</b> — теперь часть души {n}. Трогательно.",
            "🔮 <b>{title}</b> получает священную десятку. {n} преклоняется.",
            "🗿 <b>{title}</b> — 10/10. {n} сидит молча. Это высшая форма похвалы.",
            "🎆 Десятка! <b>{title}</b> теперь в личном пантеоне {n}. Редкая честь.",
        ],
    },  # конец "anime"

    # ────────────────────────────────
    #  МАНГА (свои тексты — читает, а не смотрит)
    # ────────────────────────────────

    "manga": {

        # 📋 Добавил в «Запланированное»
        "planned": [
            "📚 {n} добавил мангу <b>{title}</b> в список. Прочитает — это вопрос времени, не желания.",
            "🗂️ <b>{title}</b> записана в очередь. Полки ломятся, {n} не останавливается.",
            "📌 {n} запланировал <b>{title}</b>. Главы сами себя не прочитают.",
            "📌 <b>{title}</b> теперь в планах у {n}. Главы подождут, никуда не денутся.",
            "🔖 <b>{title}</b> зафиксирована. {n} снова расширяет свои непрочитанные владения.",
            "📥 Хоп — <b>{title}</b> теперь в планах. Сколько глав? Неважно. Прочитаю. Когда-нибудь.",
            "🔖 {n} присмотрел <b>{title}</b>. В очереди, но очередь у {n} рабочая.",
            "📖 <b>{title}</b> ждёт своего часа. {n} до неё доберётся, дайте срок.",
            "🎯 {n} закинул мангу <b>{title}</b> в планы. Том за томом — но потом.",
            "🧠 <b>{title}</b> в списке у {n}. Не свалка, просто очередь чуть длинновата 😅",
        ],

        # ▶️ Начал читать
        "watching": [
            "📖 {n} открыл мангу <b>{title}</b>. Поехали, глава за главой.",
            "🎌 {n} приступил к чтению <b>{title}</b>. Спать, видимо, не скоро.",
            "👁️ <b>{title}</b> в руках {n}. Ждём отчёта с полей.",
            "📜 {n} начал читать <b>{title}</b>. Надеемся, глав там хватит.",
            "🚀 Старт! <b>{title}</b> — новая манга в арсенале {n}.",
            "😤 {n} не устоял и взялся за <b>{title}</b>. Конца и края не видно, но кого это останавливало.",
            "📖 {n} открыл мангу <b>{title}</b>. Глава за главой, понеслось.",
            "🎌 {n} взялся за <b>{title}</b>. Спать сегодня, видимо, не план.",
            "👀 <b>{title}</b> в руках у {n}. Если пропадёт — он там, листает.",
            "📚 {n} начал читать <b>{title}</b>. Списку стало легче на одну позицию.",
            "🚀 <b>{title}</b> пошла у {n}. Посмотрим, проглотит за ночь или растянет.",
            "😎 {n} дорвался до <b>{title}</b>. «Ещё пару глав» — и так до утра.",
            "🔖 {n} приступил к <b>{title}</b>. Закладка двинулась с нулевой главы.",
            "🌙 Манга <b>{title}</b> открыта. {n} уже знает, что ляжет поздно.",
        ],

        # 🔁 Перечитывает
        "rewatching": [
            "🔁 {n} перечитывает <b>{title}</b>. Значит, оно того стоило.",
            "♻️ <b>{title}</b> снова открыта. {n} возвращается за второй дозой.",
            "🌀 Повторный заход на мангу <b>{title}</b>. Хороший знак.",
            "📚 {n} листает <b>{title}</b> по второму кругу. Некоторые детали проявляются только так.",
            "🔂 <b>{title}</b> на перечитке у {n}. Привязанность подтверждена.",
            "👏 {n} снова с <b>{title}</b> в руках. Уважаю преданность.",
            "🔁 {n} перечитывает <b>{title}</b>. Видимо, осело глубоко.",
            "♻️ <b>{title}</b> открыта повторно. {n} возвращается к проверенному.",
            "📖 {n} взялся за <b>{title}</b> по второму кругу. Детали проявляются только так.",
        ],

        # 💀 Бросил
        "dropped": [
            "🗑️ Манга <b>{title}</b> — дропнута. {n} не пощадил.",
            "💀 {n} закрыл <b>{title}</b> и больше не открывал. Всё.",
            "🚪 <b>{title}</b> осталась недочитанной. {n} ушёл без объяснений.",
            "❌ <b>{title}</b> — в архив. Минус одна манга в этом суровом мире.",
            "😤 {n} дал <b>{title}</b> шанс. Манга не оценила. Итог — дроп.",
            "🏳️ <b>{title}</b> не выдержала испытания {n}. Бывает с лучшими.",
        ],

        # ✅ Завершил без оценки
        "completed_no_score": [
            "✅ {n} дочитал мангу <b>{title}</b>. Молчит. Обрабатывает.",
            "🏁 <b>{title}</b> — прочитано. {n} ставит точку без комментариев.",
            "👀 Финальная глава <b>{title}</b> перевёрнута. Мнение {n} — тайна.",
            "📚 {n} прошёл <b>{title}</b> до конца. Оценка засекречена.",
            "🎌 <b>{title}</b> прочитана. {n} не спешит раскрываться.",
            "🤐 Дочитал и молчит. <b>{title}</b> явно оставила след.",
            "✅ {n} дочитал <b>{title}</b>. Оценку оставил при себе.",
            "🏁 <b>{title}</b> закрыта. {n} перевернул последнюю страницу без вердикта.",
            "📖 {n} добил <b>{title}</b>. Без оценки — бывает и так.",
            "📚 <b>{title}</b> прочитано. Оценку {n} приберёг, видимо."
        ],

        # ⭐ Оценка 1–3
        "completed_score_low": [
            "💩 Манга <b>{title}</b> — {score}/10. {n} дочитал из принципа. Терпеливый человек.",
            "😭 {score}/10 за <b>{title}</b>. Жертва времени принесена. Ради чего?",
            "🤮 <b>{title}</b> получает {score}/10. {n} явно не в восторге.",
            "⚰️ {score}/10 — <b>{title}</b> похоронена в памяти {n}.",
            "🧟 {n} пережил <b>{title}</b> ({score}/10). Медаль за стойкость.",
            "🔥 <b>{title}</b> — {score}/10. Сожжено, забыто, не рекомендуется.",
            "📉 <b>{title}</b> — {score}/10. {n} долистал из упрямства.",
            "🫠 {score}/10 за <b>{title}</b>. Главы кончились раньше, чем терпение. Но впритык.",
        ],

        # 😐 Оценка 4–6
        "completed_score_mid": [
            "😐 <b>{title}</b> — {score}/10. Среднячок. {n} не потрясён.",
            "🫤 {score}/10 за мангу <b>{title}</b>. Прочитал. Закрыл. Пошёл дальше.",
            "🤷 {n} поставил <b>{title}</b> {score}/10. Бывало лучше, бывало хуже.",
            "📊 <b>{title}</b> — {score}/10. В целом норм, но без огня.",
            "🌫️ {score}/10 — <b>{title}</b> прошла мимо сердца {n}.",
            "😶 Прочитал. Оценил. {score}/10. <b>{title}</b> не изменила мировоззрение.",
            "⚖️ {score}/10 за <b>{title}</b>. Прочитано, оценено, забыто к утру.",
        ],

        # 🌟 Оценка 7–9
        "completed_score_high": [
            "🌟 Манга <b>{title}</b> — {score}/10! {n} доволен. Художник постарался.",
            "🔥 {score}/10 за <b>{title}</b>! {n} явно не разочарован.",
            "👏 <b>{title}</b> — {score}/10 от {n}. Достойное чтиво.",
            "✨ {score}/10 — <b>{title}</b> зацепила {n} за живое.",
            "🎉 {score}/10 за <b>{title}</b>. Рекомендую всем любителям хорошей манги.",
            "💫 <b>{title}</b> — {score}/10. Редкий случай, когда {n} доволен.",
            "📈 <b>{title}</b> — {score}/10. {n} закрыл последнюю главу с уважением.",
            "🎯 {score}/10 за <b>{title}</b>. Крепко, до десятки чуть не дотянуло.",
        ],

        # 👑 Оценка 10
        "completed_score_perfect": [
            "👑 <b>{title}</b> — ДЕСЯТКА! {n} нашёл новый шедевр манги. Запишите.",
            "🏆 10/10! <b>{title}</b> — в пантеоне {n} навсегда.",
            "💎 {n} поставил манге <b>{title}</b> десятку. Художник может гордиться.",
            "🌌 10/10 за <b>{title}</b>. {n} дочитал и сидит в тишине. Это говорит всё.",
            "🎌 Максимум! <b>{title}</b> — теперь часть {n}. Прямо в душу.",
            "🔮 <b>{title}</b> получает священную десятку. {n} не шутит.",
            "🗿 <b>{title}</b> — 10/10. {n} дочитал и уставился в стену. Шедевр.",
            "🎆 10/10 за <b>{title}</b>. {n} такое раздаёт по большим праздникам.",
        ],

    },  # конец "manga"

    # ────────────────────────────────
    #  ОБЩИЕ — изменение оценки (для аниме и манги одинаково)
    # ────────────────────────────────
    "score_changed": [
        "🔄 {n} пересмотрел оценку <b>{title}</b>: было {old}, стало {new}. Что-то изменилось.",
        "🤔 <b>{title}</b> переоценено: {old} → {new}. {n} явно что-то переосмыслил.",
        "🏹 {old} → {new} за <b>{title}</b>. {n} дал второй шанс (или отобрал).",
        "⚖️ Весы справедливости скорректированы: <b>{title}</b> теперь {new}/10 вместо {old}.",
        "✏️ {n} исправил оценку <b>{title}</b> с {old} на {new}. Бывает, мнения меняются.",
        "📊 Обновление рейтинга: <b>{title}</b> {old} → {new}. {n} не стоит на месте.",
    ],  # конец "score_changed"

    # ────────────────────────────────
    #  ИЗБРАННОЕ — добавление в favourites
    # ────────────────────────────────
    "favourites": {

        "anime": [
            "⭐ {n} добавил <b>{title}</b> в избранное. Это не просто хорошее аниме — это особенное.",
            "💫 <b>{title}</b> теперь в избранном у {n}. Значит, зацепило по-настоящему.",
            "🏅 Особая отметка: <b>{title}</b> попало в избранное {n}. Это дорогого стоит.",
            "✨ {n} выделил <b>{title}</b> среди всех. Избранное — это серьёзно.",
            "🌟 <b>{title}</b> — в избранном. {n} не раздаёт такое направо и налево.",
            "⭐ {n} добавил <b>{title}</b> в избранное. Это уже не просто «понравилось».",
            "💫 <b>{title}</b> теперь в избранном у {n}. Зацепило так, что не отпускает.",
            "🏅 {n} выделил <b>{title}</b> среди всех. В избранное к нему попадает не каждый шедевр.",
            "🎖️ {n} отметил <b>{title}</b> как одно из любимых. Это говорит само за себя.",
            "❤️ <b>{title}</b> зацепило {n} по-настоящему — прямиком в избранное.",
            "🔮 <b>{title}</b> в избранном у {n}. Из тех, что остаются с тобой надолго.",
        ],

        "manga": [
            "⭐ {n} добавил мангу <b>{title}</b> в избранное. Художник может гордиться.",
            "💫 <b>{title}</b> теперь в избранном у {n}. Среди всей прочитанной манги — особняком.",
            "🏅 Особая отметка: манга <b>{title}</b> в избранном {n}. Это не просто хорошо.",
            "✨ {n} выделил <b>{title}</b> среди всей манги. Редкий знак уважения.",
            "🌟 <b>{title}</b> — в избранном. {n} знает толк в хорошей манге.",
            "🏅 {n} выделил <b>{title}</b> среди всей прочитанной манги. А прочитано немало.",
            "🌟 {n} занёс мангу <b>{title}</b> в избранное. Высшая полка, рядом с любимыми.",
            "❤️ <b>{title}</b> легла {n} на душу — прямиком в избранное.",
            "🖋️ {n} отметил <b>{title}</b> как одну из любимых. Художник может собой гордиться.",
        ],

        "character": [
            "❤️ {n} добавил персонажа <b>{title}</b> в избранное. Кто-то явно запал в душу.",
            "💙 <b>{title}</b> — в избранных персонажах {n}. Это симпатия серьёзная.",
            "🎭 {n} выделил <b>{title}</b> среди всех персонажей. Характер оценён.",
            "✨ <b>{title}</b> попал в избранное. {n} явно не равнодушен.",
            "🌟 Новый любимый персонаж {n} — <b>{title}</b>. Запоминаем.",
        ],

        "person": [
            "🎌 {n} добавил <b>{title}</b> в избранных людей индустрии. Уважение оказано.",
            "👏 <b>{title}</b> — в избранном у {n}. Талант замечен и отмечен.",
            "✨ {n} выделил <b>{title}</b> среди людей аниме-индустрии. Достойный выбор.",
            "🌟 <b>{title}</b> попал в избранное {n}. Вклад в аниме оценён по достоинству.",
        ],
    },  # конец "favourites"
}

# DISPLAY_NAME приходит из окружения — экранируем для HTML-шаблонов, иначе
# символы < > & в имени ломают разметку (Telegram 400). Логи/plain-ответы
# используют сырой DISPLAY_NAME, здесь — только для HTML-сообщений.
_DISPLAY_NAME_HTML = h(DISPLAY_NAME)
BROADCAST_HEADER = f"📢 <b>{_DISPLAY_NAME_HTML} говорит:</b>"


# ═══════════════════════════════════════════════════════════════════
#  ПАРСЕРЫ ОПИСАНИЙ ИСТОРИИ
# ═══════════════════════════════════════════════════════════════════

def _strip_html(text: str) -> str:
    """Удаляем HTML-теги из строки.
    Shikimori может возвращать description с тегами вроде <b>7</b> —
    без очистки регулярки не найдут число.
    """
    return re.sub(r"<[^>]+>", "", text)


def extract_score_change(description: str) -> tuple[int, int] | None:
    """
    Парсим «изменена оценка с X на Y» → возвращаем (old, new).
    Если не распознали — None.
    """
    desc = _strip_html(description)
    match = re.search(
        r"изменена\s+оценка\s+[сc]\s+(\d+)\s+на\s+(\d+)",
        desc, re.IGNORECASE
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def extract_score(description: str) -> int | None:
    """
    Пытаемся вытащить оценку из строки описания.
    Реальные форматы Shikimori (судя по тестам):
      "оценено на 9"          <- основной русский формат
      "выставил оценку 8"     <- альтернативный
      "rated 7" / "scored 7"  <- английский
    """
    desc = _strip_html(description)
    # Основной русский формат: «оценено на 9» (число может быть в <b>9</b>)
    match = re.search(r"оценено\s+на\s+(\d+)", desc, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Альтернативный русский: «выставил/выставила оценку 9»
    match = re.search(r"(?:выставил|выставила)\s+оценку\s+(\d+)", desc, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Английский: «rated 7» или «scored 7»
    match = re.search(r"(?:rated?|score[d]?)\s+(\d+)", desc, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def classify_event(description: str) -> str:
    """
    Определяем тип события по полю description из API Shikimori.
    Возвращаем ключ из суб-словаря MESSAGES[media_type].

    Реальные значения description (проверено на живом API):
      "добавлено в список"       -> planned
      "просматриваю"             -> watching
      "изменена оценка с X на Y" -> score_changed
      "смотрю"                   -> watching
      "читаю"                    -> watching
      "пересматриваю"            -> rewatching
      "перечитываю"              -> rewatching
      "брошено"                  -> dropped
      "просмотрено"              -> completed  (без оценки)
      "прочитано"                -> completed  (без оценки)
      "оценено на 9"             -> completed  (с оценкой, парсим отдельно)
    """
    desc = _strip_html(description).lower()

    # Порядок важен: специфичные — выше, чтобы не поглотил более общий паттерн

    # Score change — проверяем первым, т.к. содержит «оценка» и может пересечься
    if any(w in desc for w in ["изменена оценка", "score changed"]):
        return "score_changed"

    # Dropped — проверяем первым, т.к. «брошено» короткое и не пересекается
    if any(w in desc for w in [
        "dropped", "брошено", "бросил", "бросила", "удалил из", "удалила из",
    ]):
        return "dropped"

    # Rewatching / re-reading (пере-)
    if any(w in desc for w in [
        "rewatching", "re-reading",
        "пересматриваю", "перечитываю",
        "перечитывает", "пересматривает",
    ]):
        return "rewatching"

    # Planned — "добавлено в список" это главный реальный формат
    if any(w in desc for w in [
        "добавлено в список", "добавлено",
        "planned", "планирует",
        "добавил в планируемое", "добавила в планируемое",
        "want to watch", "want to read",
    ]):
        return "planned"

    # Watching / reading — текущий просмотр
    if any(w in desc for w in [
        "смотрю", "просматриваю", "читаю",
        "watching", "reading", "смотрит", "читает",
        "начал смотреть", "начала смотреть",
        "начал читать", "начала читать",
    ]):
        return "watching"

    # Всё остальное: "просмотрено", "прочитано", "оценено на N" -> completed
    return "completed"


# ═══════════════════════════════════════════════════════════════════
#  ПОСТРОЕНИЕ УВЕДОМЛЕНИЙ
# ═══════════════════════════════════════════════════════════════════

def build_message(entry: dict) -> str:
    """
    Формируем итоговое сообщение для одной записи истории.
    entry — объект из API /api/users/{user}/history.

    Название тайтла кликабельно (ссылка зашита в него), отдельной строки
    со ссылкой нет — единообразно с /favs и отчётами. Метку времени не
    добавляем: Telegram сам показывает время сообщения, а наличие новых записей 
    проверяется каждые 15 минут.
    """
    # Тип медиа и конкретный вид (kind) — нужны для выбора банка сообщений
    media_type, _kind = get_media_info(entry)
    bank = MESSAGES[media_type]

    # Название тайтла — предпочитаем русское, экранируем для HTML
    target = entry.get("target") or {}
    title_ru = target.get("russian") or ""
    title_en = target.get("name") or "???"
    title_text = h(title_ru if title_ru else title_en)

    # Зашиваем ссылку в название (если есть url) — кликабельно прямо в тексте
    target_url = _rel_url(target.get("url"))
    title = (f'<a href="{SHIKI_BASE_URL}{target_url}">{title_text}</a>'
             if target_url else title_text)

    description = entry.get("description", "") or ""
    event_type = classify_event(description)

    score = None

    if event_type == "score_changed":
        # Изменение оценки — берём шаблон из общего банка, не из anime/manga
        change = extract_score_change(description)
        old_score, new_score = change if change else (None, None)
        template = random.choice(MESSAGES["score_changed"])  # nosec B311  (случайный выбор шаблона сообщения — не крипта)
        text = template.format(
            n=_DISPLAY_NAME_HTML,
            title=title,
            old=old_score if old_score is not None else "?",
            new=new_score if new_score is not None else "?",
        )
    elif event_type == "completed":
        # Завершение — уточняем по оценке
        score = extract_score(description)
        if score is None:
            key = "completed_no_score"
        elif score <= 3:
            key = "completed_score_low"
        elif score <= 6:
            key = "completed_score_mid"
        elif score <= 9:
            key = "completed_score_high"
        else:
            key = "completed_score_perfect"
        template = random.choice(bank[key])  # nosec B311  (случайный выбор шаблона сообщения — не крипта)
        text = template.format(
            n=_DISPLAY_NAME_HTML,
            title=title,
            score=score if score is not None else "?",
        )
    else:
        key = event_type
        template = random.choice(bank[key])  # nosec B311  (случайный выбор шаблона сообщения — не крипта)
        text = template.format(
            n=_DISPLAY_NAME_HTML,
            title=title,
            score="?",
        )

    return text


def build_favourite_message(category: str, item: dict) -> str:
    """
    Формируем сообщение об добавлении в избранное.
    category: одна из _FAV_CATEGORIES (animes/mangas/ranobe/characters/
              people/mangakas/seyu/producers)
    item:     объект из API с полями id, name, russian, url и др.
              url может быть подставлен из titles{} вызывающей стороной
              (Favourites API сам отдаёт url=null).
    """
    # Категория API → ключ банка сообщений.
    # ranobe переиспользует банк манги; вся индустрия — банк person.
    cat_map = {
        "animes":     "anime",
        "mangas":     "manga",
        "ranobe":     "manga",
        "characters": "character",
        "people":     "person",
        "mangakas":   "person",
        "seyu":       "person",
        "producers":  "person",
    }
    bank_key = cat_map.get(category, "anime")
    templates = MESSAGES["favourites"].get(bank_key, MESSAGES["favourites"]["anime"])

    title_ru = item.get("russian") or ""
    title_en = item.get("name") or "???"
    title_text = h(title_ru if title_ru else title_en)

    # Ссылку зашиваем в название — единообразно с /favs и событиями
    url = _rel_url(item.get("url"))
    title = (f'<a href="{SHIKI_BASE_URL}{url}">{title_text}</a>'
             if url else title_text)

    text = random.choice(templates).format(n=_DISPLAY_NAME_HTML, title=title)  # nosec B311  (случайный выбор шаблона сообщения — не крипта)

    return text


# ═══════════════════════════════════════════════════════════════════
#  ФОРМАТТЕРЫ ОТЧЁТОВ (общий вид для /stats и квартального отчёта)
# ═══════════════════════════════════════════════════════════════════

def _top_dict(counter: dict, n: int) -> list[tuple[str, int]]:
    """Топ-N пар (ключ, count) по убыванию."""
    return sorted(counter.items(), key=lambda x: x[1], reverse=True)[:n]


def _fmt_counter(counter: dict, n: int, sep: str = "  ·  ") -> str:
    """'Экшен (34) · Драма (28)'. Оставлено для совместимости/коротких строк."""
    return sep.join(f"{h(k)} ({v})" for k, v in _top_dict(counter, n))


def _section_header(emoji: str, title: str) -> str:
    """Акцентированный заголовок архиблока: '━━━━━ 🎬 АНИМЕ ━━━━━' (жирный)."""
    line = "━" * 5
    return f"<b>{line} {emoji} {h(title)} {line}</b>"


def _fmt_mono_rows(pairs: list[tuple[str, int]], show_percent: bool = False,
                   total: int = 0) -> str:
    """
    Моноширинный блок с выровненными колонками и точками-лидерами:
        Экшен ······· 66  46%
        Триллер ····· 45  31%
    pairs — [(имя, число), ...] (уже отсортированные, обрезанные).
    show_percent — добавить долю от total (только если total > 0).
    Возвращает строку в <code>...</code> или '' если pairs пуст.

    Кириллица и латиница в моноширинном Telegram занимают 1 знак,
    поэтому выравнивание по len() корректно.
    """
    if not pairs:
        return ""
    name_w = max(len(name) for name, _ in pairs)
    num_w  = max(len(str(c)) for _, c in pairs)
    rows = []
    for name, count in pairs:
        dots = "·" * (name_w - len(name) + 1)
        num_str = str(count).rjust(num_w)
        line = f"{name} {dots} {num_str}"
        if show_percent and total > 0:
            line += f"  {round(count / total * 100)}%"
        rows.append(line)
    return f"<code>{h(chr(10).join(rows))}</code>"


def _top_block(emoji: str, title: str, counter: dict, n: int,
               show_percent: bool = False, total: int = 0) -> list[str]:
    """
    Полный блок топа: заголовок-строка + моноширинные колонки.
    Возвращает список строк (для extend) или [] если counter пуст.
    """
    pairs = _top_dict(counter, n)
    if not pairs:
        return []
    body = _fmt_mono_rows(pairs, show_percent=show_percent, total=total)
    if not body:
        return []
    return [f"{emoji} <b>{h(title)}</b>", body]


def _fmt_kinds(kinds: dict, labels: dict) -> str:
    """Разбивка по типам: 'Сериалы 95 · Фильмы 12 · OVA 8'.
    Порядок — как в labels (tv/movie/ova/ona), неизвестные kind в конце.
    Возвращает '' если данных нет.
    """
    if not kinds:
        return ""
    parts = []
    # Сначала известные типы в порядке labels
    for key, name in labels.items():
        cnt = kinds.get(key, 0)
        if cnt:
            parts.append(f"{name} {cnt}")
    # Затем неизвестные (на случай если API подкинет новый kind)
    for key, cnt in kinds.items():
        if key not in labels and cnt:
            parts.append(f"{h(key)} {cnt}")
    return "  ·  ".join(parts)


def _fmt_score_dist(dist: dict) -> str:
    """Распределение оценок без нулей (0 = без оценки): '10×8 · 9×15'.
    Оставлено для обратной совместимости; в отчётах теперь используется
    вертикальный блок _score_dist_block.
    """
    pairs = [(int(s), c) for s, c in dist.items() if _safe_int(s) > 0]
    if not pairs:
        return "нет оценок"
    return "  ·  ".join(f"{s}×{c}" for s, c in sorted(pairs, reverse=True))


def _score_dist_block(dist: dict) -> list[str]:
    """
    Вертикальный блок распределения оценок:
        📊 Оценки
        ★10 ·· 5
         ★9 ·· 8
         ★8 · 19
    Оценка помечена ★, точки — лидеры к количеству (как в остальных блоках).
    Порядок — по убыванию оценки (10 → 1), не по количеству.
    Возвращает [] если оценок нет.
    """
    pairs = [(_safe_int(s), c) for s, c in dist.items() if _safe_int(s) > 0]
    if not pairs:
        return []
    pairs.sort(key=lambda x: x[0], reverse=True)
    # Ключ — '★N', выровняем по ширине самой длинной метки (★10 шире ★9)
    rows = [(f"★{score}", count) for score, count in pairs]
    body = _fmt_mono_rows(rows)
    return ["📊 <b>Оценки</b>", body] if body else []


def _status_block_anime(agg: dict) -> list[str]:
    """Вертикальный блок статусов для аниме."""
    rows = [
        ("Завершено", agg.get("total_completed", 0)),
        ("Брошено",   agg.get("total_dropped", 0)),
        ("Смотрю",    agg.get("total_watching", 0)),
        ("В планах",  agg.get("total_planned", 0)),
        ("Отложено",  agg.get("total_on_hold", 0)),
    ]
    rows = [(n, c) for n, c in rows if c]  # скрываем нулевые
    body = _fmt_mono_rows(rows)
    return ["📦 <b>Статусы</b>", body] if body else []


def _status_block_manga(agg: dict) -> list[str]:
    """Вертикальный блок статусов для манги."""
    rows = [
        ("Прочитано", agg.get("total_completed", 0)),
        ("Брошено",   agg.get("total_dropped", 0)),
        ("Читаю",     agg.get("total_watching", 0)),
        ("В планах",  agg.get("total_planned", 0)),
        ("Отложено",  agg.get("total_on_hold", 0)),
    ]
    rows = [(n, c) for n, c in rows if c]
    body = _fmt_mono_rows(rows)
    return ["📦 <b>Статусы</b>", body] if body else []


def _kinds_block(kinds: dict, labels: dict) -> list[str]:
    """Вертикальный блок типов (Сериалы/Фильмы/OVA или Манга/Манхва/...)."""
    if not kinds:
        return []
    pairs = []
    for key, name in labels.items():
        cnt = kinds.get(key, 0)
        if cnt:
            pairs.append((name, cnt))
    for key, cnt in kinds.items():
        if key not in labels and cnt:
            pairs.append((str(key), cnt))
    body = _fmt_mono_rows(pairs)
    return ["🎞 <b>Типы</b>", body] if body else []


def _avg_score_from_dist(dist: dict) -> float | None:
    """Средняя оценка из распределения (игнорируя 0 = без оценки)."""
    total = count = 0
    for s, c in dist.items():
        sv = _safe_int(s)
        if sv > 0:
            total += sv * c
            count += c
    return round(total / count, 2) if count else None


def _title_link_from_rec(tid: str, rec: dict) -> str:
    """HTML-ссылка из записи titles{}."""
    title = h(rec.get("title") or "???")
    url = _rel_url(rec.get("url"))
    return f'<a href="{SHIKI_BASE_URL}{url}">{title}</a>' if url else title


def _pct_diff(curr: int, prev: int) -> str:
    """'↑ 25% (9 → 12)'."""
    if prev == 0:
        return f"+{curr}" if curr else "~"
    delta = curr - prev
    if delta == 0:
        return f"→ без изменений ({curr})"
    pct = round(abs(delta) / prev * 100)
    return f"{'↑' if delta > 0 else '↓'} {pct}% ({prev} → {curr})"


def _fav_lines(items: list[dict]) -> list[str]:
    """Строки одного блока избранного: '• <ссылка> — ⭐9' (оценка опц.)."""
    lines = []
    for it in items:
        title = h(it.get("title") or "???")
        url = _rel_url(it.get("url"))
        name = f'<a href="{SHIKI_BASE_URL}{url}">{title}</a>' if url else title
        score = it.get("score")
        if isinstance(score, int) and score > 0:
            lines.append(f"  • {name} — ⭐{score}")
        else:
            lines.append(f"  • {name}")
    return lines


# ═══════════════════════════════════════════════════════════════════
#  /status — ФОРМАТ СТРОКИ ОЦЕНКИ
# ═══════════════════════════════════════════════════════════════════

def format_rate_entry(item: dict, media: str) -> str:
    """Форматирует одну запись из rates API в строку для сообщения."""
    # Название тайтла — в rates API вложено в item["anime"] или item["manga"]
    target = item.get(media) or {}
    title_ru = target.get("russian") or ""
    title_en = target.get("name") or "???"
    title = h(title_ru if title_ru else title_en)

    status = item.get("_status", "")
    # Иконка в зависимости от статуса
    icon = {
        "watching":   "▶️",
        "rewatching": "🔁",
    }.get(status, "•")

    url = target.get("url", "")
    if url:
        return f'{icon} <a href="{SHIKI_BASE_URL}{url}">{title}</a>'
    return f"{icon} {title}"


# ═══════════════════════════════════════════════════════════════════
#  СТАРТОВЫЙ HEALTH-СНАПШОТ (owner-gate)
# ═══════════════════════════════════════════════════════════════════
#
#  Пинг '🟢 Бот запущен' уходит ДО первого синка (owner-gate), поэтому
#  времена берутся от ПРОШЛОГО запуска — их протухлость и есть диагностика.
#  Полный вайп (нет seen_ids И нет метки stats_all) схлопывается в один
#  явный баннер вместо трёх «нет данных». Чистая функция: принимает
#  значения-как-с-диска, возвращает готовый ПЛОСКИЙ текст (без HTML —
#  шлётся как есть, тем же каналом, что и голый пинг).


def _parse_iso(value: str | None) -> datetime | None:
    """ISO-строка -> naive-UTC datetime; кривое/пустое -> None. tzinfo срезаем:
    updated_at от _utcnow всегда naive, но защищаемся от tz-aware входа, иначе
    _human_ago упадёт на 'naive - aware' вычитании."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _parse_ts(value: float | None) -> datetime | None:
    """epoch-секунды (time.time) -> наивный-UTC datetime; кривое -> None."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _fmt_moment(dt: datetime | None) -> str:
    """'02.07.2026 14:30 (2 ч назад)' или 'нет данных', если времени нет."""
    if dt is None:
        return "нет данных"
    return f"{_fmt_dt_short(dt)} ({_human_ago(dt)})"


def build_startup_snapshot(
    *,
    display_name: str,
    shiki_user: str,
    check_interval_sec: int,
    subscriber_count: int,
    seen_ids_count: int,
    seen_favs_count: int,
    stats_updated_at: str | None,
    last_backup_at: float | None,
) -> str:
    """Собирает текст стартового пинга владельцу с health-снапшотом.

    Состояния:
      • норма            — отслеживание активно, свежесть синка/бэкапа;
      • не инициализ.    — есть stats_all, но нет seen_ids (события за
                           простой уйдут в тишину — предупреждаем);
      • полный вайп      — нет seen_ids И нет метки stats_all: один баннер
                           «чистый инстанс» вместо трёх «нет данных».
    """
    minutes = max(1, check_interval_sec // 60)
    lines = [
        "🟢 Бот запущен",
        "",
        f"👤 Имя: {display_name} · Шики-логин: {shiki_user} · "
        f"⏱ проверка каждые {minutes} мин",
        "",
        f"👥 Подписчиков: {subscriber_count}",
    ]

    stats_dt = _parse_iso(stats_updated_at)

    # Полный вайп: состояние отсутствует целиком — один громкий сигнал.
    if seen_ids_count == 0 and stats_dt is None:
        lines.append("")
        lines.append("🆕 Чистый инстанс — состояние отсутствует (том пуст / вайп)")
        lines.append("     Первый запуск: события за прошлый простой не догоним")
        return "\n".join(lines)

    # Строка отслеживания несёт здоровье-смысл, а не голое число.
    if seen_ids_count == 0:
        lines.append(
            "🗂 ⚠️ Отслеживание не инициализировано — "
            "события за простой уйдут в тишину"
        )
    else:
        lines.append(
            f"🗂 Отслеживание: история {seen_ids_count}, "
            f"избранное {seen_favs_count} — события за простой догоним"
        )

    lines.append("")
    lines.append(f"📊 Последняя синхронизация статистики: {_fmt_moment(stats_dt)}")
    lines.append(f"💾 Последний плановый бэкап: {_fmt_moment(_parse_ts(last_backup_at))}")
    return "\n".join(lines)