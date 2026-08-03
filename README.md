# skrepka 📎

ИИ-соавтор в Google Документах. Агент работает из вашего терминала прямо в документе: читает комментарии, отвечает на них и правит текст на месте.

[In English](https://github.com/slvfmts/skrepka/blob/main/README.en.md) · [Приватность](https://github.com/slvfmts/skrepka/blob/main/PRIVACY.md) · [Безопасность](https://github.com/slvfmts/skrepka/blob/main/SECURITY.md)

## Зачем

Работа над текстом с ИИ обычно идёт в двух окнах: документ и чат с агентом. Скопировать абзац в чат, вставить ответ обратно, руками починить оформление. Пока текст ходит туда-сюда, теряются правки и комментарии.

skrepka убирает это копирование. Вы оставляете по тексту обычные комментарии: про структуру, логику, формулировки. Потом зовёте агента в документ, он читает треды, отвечает и правит текст на месте. Заказчик приходит, комментирует там же, работа продолжается тем же способом. Комментарии при этом не рвутся: якоря остаются на месте, а закрывает треды только человек.

## Для кого

Редакторам, копирайтерам и контент-менеджерам, которые ведут и согласовывают тексты в Google Docs, а к работе подключают ИИ-агента: Claude Code, Codex или другого. Быть разработчиком не нужно, доступ к Google настраивает мастер `skrepka init`.

## Что умеет

Главный сценарий — разобрать комментарии. Вы говорите агенту «отработай комментарии», он читает треды, отвечает по делу и вносит правки в текст.

Кроме этого skrepka выгружает документ в markdown и заливает правки обратно, создаёт документы из markdown и разбирает предложенные правки. Полный список сценариев — в [docs/PLUGIN.md](https://github.com/slvfmts/skrepka/blob/main/docs/PLUGIN.md).

## Как начать

1. Поставьте skrepka: `pipx install skrepka`.
2. Настройте доступ к Google: `skrepka init`. Первая настройка занимает 15–30 минут, инструкция со снимками экрана — в [docs/QUICKSTART.md](https://github.com/slvfmts/skrepka/blob/main/docs/QUICKSTART.md).
3. Подключите навыки к агенту: [docs/PLUGIN.md](https://github.com/slvfmts/skrepka/blob/main/docs/PLUGIN.md).

Дальше попросите агента отработать комментарии в тестовом документе.

Вы заводите собственный проект Google Cloud и работаете от своего имени. У skrepka нет сервера и телеметрии: автор skrepka ваших документов и токенов не видит. Подробности в [PRIVACY.md](https://github.com/slvfmts/skrepka/blob/main/PRIVACY.md).

## Документация

| Файл | Что внутри |
|---|---|
| [docs/QUICKSTART.md](https://github.com/slvfmts/skrepka/blob/main/docs/QUICKSTART.md) | Настройка доступа к Google по шагам |
| [docs/PLUGIN.md](https://github.com/slvfmts/skrepka/blob/main/docs/PLUGIN.md) | Навыки для Claude Code и Codex |
| [docs/LIMITATIONS.md](https://github.com/slvfmts/skrepka/blob/main/docs/LIMITATIONS.md) | Что skrepka не делает |
| [PRIVACY.md](https://github.com/slvfmts/skrepka/blob/main/PRIVACY.md) | Какие данные и куда идут |
| [SECURITY.md](https://github.com/slvfmts/skrepka/blob/main/SECURITY.md) | Модель угроз и как сообщить об уязвимости |

## Лицензия

MIT
