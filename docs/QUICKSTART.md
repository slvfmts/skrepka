# QUICKSTART: авторизация в Google

Этот гайд предполагает, что skrepka уже установлена. Если ещё нет, сначала установите её по [README](../README.md).

skrepka работает от вашего имени через ваш проект Google Cloud и ваш OAuth-клиент. Первая настройка занимает 15–30 минут, дальше skrepka работает сама.

## Что понадобится

Аккаунт Google (личный Gmail подойдёт).
Браузер и терминал.
15–30 минут на первую настройку.

Замечание. Google периодически меняет консоль, поэтому кнопки могут стоять чуть иначе, чем на снимках. Общий смысл остаётся тем же.

## Шаг 1. Создать проект Google Cloud

Откройте <https://console.cloud.google.com/projectcreate>, войдите в нужный аккаунт в Gmail. Задайте имя проекта (любое, например `Skrepka`) и нажмите Create. Затем выберите этот проект в переключателе вверху страницы. Все следующие шаги должны идти в нём.

![Создание проекта](img/quickstart/01-create-project.png)

## Шаг 2. Включить Google Docs API

Откройте <https://console.cloud.google.com/apis/library/docs.googleapis.com> (каждый раз при переходе по ссылке Google может просить авторизоваться в аккаунте заново) и нажмите Enable.

![Включение Google Docs API](img/quickstart/02-enable-docs-api.png)

## Шаг 3. Включить Google Drive API

Откройте <https://console.cloud.google.com/apis/library/drive.googleapis.com> и нажмите Enable.

![Включение Google Drive API](img/quickstart/03-enable-drive-api.png)

Важно про доступ. Включить API значит разрешить проекту обращаться к службе, а не выдать доступ к вашим файлам.

## Шаг 4. Настроить экран согласия и доступ

Откройте <https://console.cloud.google.com/auth/overview> (проверьте, что вверху указан ваш проект).

### 4.1. Пройти мастер «Get started»

На новом проекте платформа ещё не настроена. Нажмите Get started.

![Google Auth Platform — Get started](img/quickstart/04-get-started.png)

Мастер проведёт через несколько коротких экранов.

App Information. App name: любое (например `Skrepka`; это имя увидите на экране согласия при входе). User support email: ваш адрес.
Audience. Выберите External, единственный вариант для личного Gmail.

  ![Audience — External](img/quickstart/05-audience-external.png)

Contact Information. Ваш email для уведомлений Google о проекте.
Finish. Поставьте галочку согласия с политикой и нажмите Continue → Create.

  ![Finish — согласие с политикой](img/quickstart/06-finish-agree.png)

### 4.2. Добавить один доступ (scope)

Слева откройте вкладку Data Access → Add or remove scopes. В поле «Manually add scopes» вставьте ровно один scope, нажмите Add to table, затем Update:
```
https://www.googleapis.com/auth/drive
```

Больше ничего не добавляйте: одного `drive` достаточно и для работы с документами, и для комментариев.

![Добавление scope drive](img/quickstart/07-add-drive-scope.png)

Появится окно «Verification required», просто нажмите Continue. Поля про justification, demo-видео и предложение отправить приложение на проверку (verification) заполнять и отправлять не нужно: для личного использования проверка Google не требуется (см. 4.4).

![Verification required — Continue](img/quickstart/08-verification-popup.png)

### 4.3. Опубликовать приложение (Publish app)

Слева откройте Audience. Если Publishing status = Testing, нажмите Publish app и подтвердите Confirm в окне «Push to production».

![Publish app](img/quickstart/09-publish-app.png)

![Push to production — Confirm](img/quickstart/10-push-to-production.png)

Зачем: в режиме Testing вход протухает каждые 7 дней. В Production он живёт долго, а «непроверенным» приложение при этом остаётся, для личного использования этого достаточно.

### 4.4. Плашку «требуется верификация» можно игнорировать

Из-за доступа `drive` консоль будет предлагать отправить приложение на проверку Google.

Для личного использования это не нужно: вы пользуетесь своим приложением сами. Единственное следствие: на шаге 6 при входе Google покажет экран «приложение не проверено».

## Шаг 5. Создать OAuth-клиент (Desktop)

Слева откройте Clients → Create client → Application type = Desktop app → задайте имя → Create.

![Создание Desktop-клиента](img/quickstart/11-create-desktop-client.png)

В открывшемся окне нажмите Download JSON, сохранится файл вида `client_secret_….json`. Держите его в надёжном месте и не коммитьте в git.

![Скачать JSON](img/quickstart/12-download-json.png)

## Шаг 6. Запустить `skrepka init`

В терминале наберите команду с пробелом в конце и не жмите Enter:

```bash
skrepka init --credentials 
```

Дальше нужно подставить путь к скачанному `client_secret_….json`. На macOS проще всего перетащить файл из Finder прямо в окно терминала, и путь подставится сам. Иначе вставьте путь к файлу вручную. Теперь нажмите Enter.

Откроется браузер для входа в Google. Вы увидите предупреждение, что приложение не проверено Google. Это ожидаемо для вашего собственного клиента. Нажмите Advanced (внизу слева), затем ссылку «Go to … (unsafe)». Кнопку «Back to safety» не нажимайте, она отменяет вход.

![Приложение не проверено — Advanced → Go to … (unsafe)](img/quickstart/13-unverified-advanced.png)

На экране согласия предоставьте запрошенный доступ (один пункт про файлы Google Диска) и нажмите Continue.

![Экран согласия — Continue](img/quickstart/14-consent-allow.png)

В конце skrepka сделает быструю проверку (создаст тест-документ, добавит комментарий, приберёт за собой) и выведет `All set.` На этом настройка окончена.

Проверьте результат командой:

```bash
skrepka doctor
```

Все проверки должны быть `[ok]`.

![skrepka doctor — всё ok](img/quickstart/15-doctor-ok.png)

## Типичные проблемы

Вход протухает через 7 дней. Значит, проект остался в режиме Testing. Опубликуйте его (Шаг 4.3) и выполните `skrepka init` заново.
«…API is not enabled». Вернитесь к Шагам 2–3 и включите нужный API, затем повторите. После включения API может пройти минута-другая, прежде чем он заработает.
`command not found: skrepka`. Терминал не знает, где лежит команда. Используйте полный путь, например `~/.local/bin/skrepka init --credentials …`.
Скачали не тот клиент. Нужен именно Desktop app. Service-account ключ или Web-клиент skrepka отклонит с подсказкой.

## Что дальше

Сейчас у агента есть доступ к вашим документам, но он ещё не умеет с ними работать. Подключите навыки skrepka к агенту по инструкции [PLUGIN.md](PLUGIN.md). Они научат Claude Code или Codex читать комментарии, отвечать на них и вносить правки.

Проверьте связку на живом примере: создайте тестовый документ, оставьте в нём пару комментариев и попросите агента их отработать. Без этого шага у вас настроенный доступ, но ещё не соавтор.

Что происходит с вашими данными после авторизации, описано в [PRIVACY.md](../PRIVACY.md). Отозвать доступ и удалить локальные данные помогут команды `logout` / `revoke` / `forget`, они описаны там же.
