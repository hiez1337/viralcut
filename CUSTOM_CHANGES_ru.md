# Журнал кастомных изменений (Custom Changes Log)

*[English version](CUSTOM_CHANGES.md)*

Здесь фиксируются все персональные модификации, внесённые поверх ветки `upstream/main` (`vanwild11/viralcut`).
Когда автор оригинального репозитория выпускает крупные обновления или проводит рефакторинг, LLM-агент может использовать этот журнал, чтобы аккуратно перенести или адаптировать наши функции на новую ветку `main`.

## Начальная настройка (Setup)

- Первоначальная настройка форка на ветке `custom` (`AGENTS.md`, `CUSTOM_CHANGES.md`).

## Увеличенная модель Whisper по умолчанию + CUDA-ускорение на Windows
- **Цель**: Оптимизировать форк под персональный ПК на Windows (Ryzen 7 5700X3D, RTX 4060 8 GB VRAM) с GPU-ускорением Whisper (`cuda` `float16` → `int8_float16` → `cpu` при откате) и моделью `large-v3` по умолчанию.
- **Новые файлы**:
  - `viralcut/custom_whisper.py`: константа `DEFAULT_WHISPER_MODEL = "large-v3"`, функции `resolve_whisper_model()`, `register_cuda_dlls()` (регистрирует директории с DLL `nvidia/*/bin` из pip на Windows) и `whisper_candidates()`.
  - `requirements-custom.txt`: расширяет `requirements-desktop.txt` зависимостями `nvidia-cublas-cu12` и `nvidia-cudnn-cu12`.
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/config.py`: минимальный импорт и вызов `WHISPER_MODEL = resolve_whisper_model()`.
  - `viralcut/transcribe.py`: замена встроенного списка кандидатов устройств на `whisper_candidates(long_audio, WHISPER_DEVICE, WHISPER_COMPUTE)`.
  - `run_windows.bat`: установка `requirements-custom.txt` вместо `requirements-desktop.txt`.
  - `README.md`: добавлено уведомление о персональном форке и аппаратной оптимизации в самом начале.
- **Проверка работоспособности**:
  - `.\.venv-win\Scripts\python.exe -c "from viralcut import config; assert config.WHISPER_MODEL == 'large-v3'"`
  - Проверка, что `whisper_candidates(False, "auto", "int8")` начинается с `("cuda", "float16")` и библиотеки CUDA DLL успешно загружаются.
  - Запуск `.\.venv-win\Scripts\python.exe -m unittest discover -s tests`.

## Выбор качества YouTube и исправление скачивания фрагментов (1080p в эконом-режиме)
- **Цель**: Устранить две ошибки, из-за которых выбранное качество 1080p для YouTube в экономном режиме (по отрезкам / segments) незаметно сбрасывалось до 360p (формат `18`) и апскейлилось во время рендера:
  1. Функция `downloader.download_section()` жестко фиксировала `player_client: ["android", "tv"]` вместо последовательного опроса клиентов, начиная с `default`.
  2. Функция `downloader._pick_format()` отдавала приоритет `avc1`/`mp4`/сведённому аудио перед высотой кадра (`height`), позволяя потокам 360p с аудио обходить адаптивные видеопотоки 1080p.
- **Новые файлы**:
  - `viralcut/custom_downloader.py`: `pick_best_format()` (строгое ранжирование по высоте с предпочтением прямого HTTPS перед HLS при равном разрешении и кодеке) и `download_section_smart()` (опрашивает клиентов YouTube через `_youtube_info`, фиксирует реальное разрешение и причину отката, передавая данные в `custom_debug`).
  - `tests/test_custom_features.py`: модульные тесты для выбора форматов, отката клиентов при скачивании фрагментов и отладочной телеметрии.
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/downloader.py`: делегирует вызовы `_pick_format()` и `download_section()` (с добавлением опционального `meta_out`) в `custom_downloader`.
  - `viralcut/pipeline.py`: передаёт `meta_out` и `force_hd` в `_process_segments()` и сохраняет метаданные качества источника через `db.merge_meta()`.
- **Проверка работоспособности**:
  - Запуск `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.
  - Скачивание 3-секундного отрезка `https://youtu.be/9-2Gz9343hQ` с `download_section(..., max_height=1080, meta_out=meta)` с проверкой, что реальная высота `== 1080` и `fallback == False`.

## Нативная панель живого инспектора отладки (Live Debug Inspector)
- **Цель**: Добавить нативную тёмную панель-инспектор в стиле iOS dark-glass (`Инспектор`, `Ctrl+Shift+D`, открывается из блока `#health` / `.side-foot`), отображающую в реальном времени нагрузку GPU/VRAM, системный и процессный CPU & RAM, модель/устройство/скорость Whisper (включая рост незавершённого файла `.incomplete` на диске NTFS), предупреждения о клиенте/формате/апскейле YouTube, таймер активной стадии проекта и дочерние процессы `ffmpeg`.
- **Новые файлы**:
  - `viralcut/custom_debug.py`: потокобезопасное хранилище телеметрии + кэширующий сэмплер оборудования с интервалом 1 с (типизированные вызовы Win32 ctypes для CPU/RAM/размеров файлов + `nvidia-smi`).
  - `web/custom_debug.css`: стили стеклянной панели и сетки показателей, согласованные с переменными `web/styles.css`.
  - `web/custom_debug.js`: изолированный UI + опрос сервера без наложения запросов только тогда, когда панель открыта и видима.
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/main.py`: добавлен эндпоинт `GET /api/debug`, возвращающий `custom_debug.snapshot()`.
  - `viralcut/jobs.py`: добавлен хелпер `active_project_ids()`.
  - `viralcut/downloader.py`, `viralcut/transcribe.py`, `viralcut/render.py`: минимальные хуки, передающие прогресс загрузки, устройство/статус Whisper и PID/прогресс дочерних процессов `ffmpeg`.
  - `web/index.html`: подключены скрипт и стили `/static/custom_debug.css?v=1` и `/static/custom_debug.js?v=1`.
- **Проверка работоспособности**:
  - `.\.venv-win\Scripts\python.exe -c "from viralcut import main; d = main.debug_status(); assert d['server']['ram_mb'] > 0 and d['gpu']['available']"`
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`

## Отключение автопроверки обновлений по умолчанию + переключатель в интерфейсе
- **Цель**: В оригинале при каждом запуске проверяются релизы `vanwild11/viralcut` на GitHub. Для персонального форка проверка отключена по умолчанию, а в интерфейс (`.side-foot` и подвал Инспектора) вынесен явный переключатель (`Автообнова: выкл/вкл`).
- **Новые файлы**:
  - `viralcut/custom_updater.py`: константа `DEFAULT_UPDATE_CHECK = False` и функция `resolve_update_enabled()` (учитывает переменную окружения `VIRALCUT_UPDATE_CHECK` и сохранённое состояние в `data/update.json`).
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/updater.py`: делегирует вызов `enabled()` в `custom_updater.resolve_update_enabled()` и делает сохранение `_save()` атомарным через замену файла `.tmp`.
  - `web/custom_debug.js`, `web/custom_debug.css`: отрисовывает переключатель `Автообнова: выкл` / `Автообнова: вкл`, синхронизированный с `/api/update` и `/api/update/settings`.
- **Проверка работоспособности**:
  - `.\.venv-win\Scripts\python.exe -c "from viralcut import updater; print(updater.payload()['enabled'])"` (возвращает `False` без сетевых запросов к GitHub, если не настроено иначе).
  - Запуск `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.

## Правки по итогам аудита: стабильность, утечки ресурсов, перерендер эконом-клипов и полировка предпросмотра
- **Цель**: Исправить критические ошибки рендеринга и многопоточности, утечки памяти/диска/VRAM, а также недочёты интерфейса предпросмотра, выявленные в ходе детального аудита.
- **Изменённые кастомные файлы**:
  - `viralcut/custom_downloader.py`: проброс исключения `Cancelled` в `download_section_smart()`; добавлена функция `rerender_section_clip()`, позволяющая корректно перерендерить клип в экономном режиме (`segments=True`) повторным скачиванием только его диапазона.
  - `viralcut/custom_fonts.py`: защита синхронизации `sync()` через `_SYNC_LOCK`, пропуск повторного копирования неизменённых файлов на Windows, атомарное копирование через `.tmp`, закрытие `TTFont` через контекстный менеджер `with`.
  - `viralcut/custom_debug.py`, `web/custom_debug.js`, `web/custom_debug.css`: ужесточена блокировка `_init_win32()`, предотвращены состояния гонки для `_MODEL_DL_PREV` и `.stat()` на файлах `.incomplete`, добавлен кулдаун при отсутствии `nvidia-smi`, округлена температура GPU, отображается реальное устройство Whisper в режиме ожидания (`cuda`/`float16`), прерываются незавершённые запросы опроса при закрытии/паузе, а закрытая панель скрывается от фокуса клавиатуры и скринридеров (`visibility: hidden`, `inert`, `aria-hidden`).
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/db.py`: перевод `connect()` в контекстный менеджер `@contextmanager`, гарантированно вызывающий `conn.close()` в блоке `finally:` (устранена утечка дескрипторов SQLite при каждом опросе).
  - `viralcut/render.py`: вынесена функция `_run_render_proc()`, благодаря чему при откате с аппаратного энкодера на `libx264` сохраняются завершающие аргументы (`-r 30 ... <out_path>`) и вычитывается `stdout`, предотвращая дедлок пайпа.
  - `viralcut/transcribe.py`: общий слот кэша модели для Windows и Linux (предотвращает двойное выделение ~6 ГБ VRAM для `large-v3`), гарантированное удаление файлов `audio16k.wav` и `whisper_chunks_*` в блоке `finally:`, проброс `Cancelled`.
  - `viralcut/downloader.py`, `viralcut/pipeline.py`, `viralcut/jobs.py`, `viralcut/live.py`, `viralcut/reframe.py`: проброс `Cancelled`, добавлена блокировка `_CANCEL_LOCK`, сериализация отмены повторов через `jobs.submit_retry()`, очистка временных файлов `sections/` и `win_*.wav` в `finally:`, предотвращение утечки `_STOP_EVENTS` для не-live проектов, кэширование XML каскада Хаара, поддержка перерендера в эконом-режиме через `custom_downloader.rerender_section_clip()`.
  - `viralcut/main.py`: заголовок кэширования для `/static/` изменён на `no-cache`, потоковое чтение загружаемых шрифтов с ограничением `MAX_FONT_BYTES`, удаление рабочей директории `/api/upload` при любых сбоях, скрытие чисто аудио-форматов из `source_url`, предварительная валидация `clip_rerender`, согласованная и безопасная отмена задач при удалении или повторе.
  - `web/index.html`, `web/app.js`, `web/styles.css`: освобождение декодеров `<video>` перед заменой DOM, устранение двойного рендера и шторма запросов с кодом `206` (`preload="metadata"`) при открытии проекта, кэширование постеров по `updated_at`, исправление относительной перемотки и широких полей таймкодов в редакторе клипов, размещение кнопок `Скачать MP4` и `SRT` рядом друг с другом, добавление паузы по клику, предпросмотра хука, синхронизации соотношения сторон, `ResizeObserver` и защиты от потери несохранённых данных в предпросмотре шаблонов.
- **Проверка работоспособности**:
  - Запуск `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.

## Учёт дискового пространства и раздельное управление очисткой в Инспекторе
- **Цель**: Отображать использование диска по категориям (`клипы`, `временные`, `исходники`, `модели`) и предоставить два безопасных элемента управления очисткой в Инспекторе (`Очистить временные` и `Удалить исходники`) без риска затронуть модели Whisper, готовые клипы, проекты в статусе `review` или активные задачи.
- **Новые файлы**:
  - `viralcut/custom_cleanup.py`: кэшируемый расчёт `storage_summary()`, защита активных проектов и запуск очистки `run_cleanup(scope="temp" | "sources")`.
- **Изменённые кастомные файлы**:
  - `viralcut/custom_debug.py`: добавление секции `"storage": custom_cleanup.storage_summary()` в `snapshot()`.
  - `web/custom_debug.js`, `web/custom_debug.css`: добавлена карточка `Диск и очистка` с обеими кнопками и диалогом подтверждения для удаления `исходников`.
  - `tests/test_custom_features.py`: добавлен тестовый класс `CustomCleanupTest`.
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/main.py`: добавлена схема `CleanupIn` и эндпоинт `POST /api/debug/cleanup`.
  - `web/index.html`: увеличена версия ассетов `custom_debug` до `?v=3`.
- **Проверка работоспособности**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.
  - Открыть `Инспектор` по адресу `http://localhost:8765/` и убедиться, что в блоке `Диск и очистка` отображаются точные размеры и корректные состояния активности для обеих кнопок.

## Полная переработка README форка и очистка метаданных GitHub
- **Цель**: Переписать `README.md` с нуля специально для форка `hiez1337/viralcut`, чтобы посетители и пользователи не вводились в заблуждение ссылками на релизы upstream, плашками/баннерами `@ivanAI22`, ссылками на `vanwild11.github.io`, старыми моделями по умолчанию (`small`) или устаревшими инструкциями. Обновить метаданные репозитория GitHub (`default_branch=custom`, персональное описание форка, удаление чужого homepage URL).
- **Изменённые файлы**:
  - `README.md`: полное обновление с описанием ключевых преимуществ форка, настроек CUDA/`large-v3`/NVENC по умолчанию, Инспектора и очистки диска, отключённых по умолчанию автообновлений, актуальных переменных окружения, эндпоинтов API, запуска из исходников (`run_windows.bat` / `./run.sh`), схемы веток и чёткого разграничения с оригинальным `vanwild11/viralcut`.
  - `CUSTOM_CHANGES.md`, `CUSTOM_CHANGES_ru.md`: задокументировано обновление документации и метаданных репозитория.
- **Проверка работоспособности**:
  - Проверка, что в `README.md` отсутствуют устаревшие упоминания `1.3.0` или `@ivanAI22`, а все ссылки ведут на локальные файлы или репозиторий `hiez1337/viralcut` (с упоминанием `vanwild11/viralcut` исключительно в целях атрибуции автора оригинала и описания механизма обновлений).
  - Проверка через `gh repo view hiez1337/viralcut --json defaultBranchRef,description,homepageUrl`, подтверждающая ветку `custom`, новое описание форка и очищенный `homepageUrl`.

## Whisper `small` по умолчанию + переключатель моделей в интерфейсе (`small` / `medium` / `large-v3`) и прогресс загрузки
- **Цель**: Вернуть `small` в качестве модели Whisper по умолчанию (оптимально для CPU и облегчённых сборок Lite), дать пользователям возможность заранее оценить размер модели и требования к «железу», переключаться между `small`, `medium` и `large-v3` прямо на главном экране (выбор сохраняется в `data/whisper.json`), скачивать модели заранее с отображением живого прогресс-бара, скорости и оставшегося времени, а также безопасно переключаться на CPU при отсутствии DLL CUDA/cuDNN.
- **Новые файлы**:
  - `web/custom_whisper.css`, `web/custom_whisper.js`: изолированные карточки выбора моделей, статусные бейджи (`Готова` / `↓ ~XXX МБ` / `Загрузка XX%` / `Ошибка`), кнопка предварительного скачивания, живой прогресс-бар, индикатор режима работы GPU/CPU и плашка с предупреждением.
- **Изменённые кастомные файлы**:
  - `viralcut/custom_whisper.py`: установка `DEFAULT_WHISPER_MODEL = "small"`, добавление каталога `WHISPER_CATALOG`, сохранение настроек в `whisper.json` (`resolve_whisper_model()`, `get_active_model()`, `set_active_model()`), функции `resolve_model_ref()`, безопасная проверка наличия DLL `_cuda_dlls_available()` / `cuda_diagnostics()` (включая каталоги `sys._MEIPASS/nvidia/*/bin` и системную переменную `CUDA_PATH`), дедуплицированная загрузка `ensure_model_downloaded()` / `start_model_download()` с независимым `ProgressTqdm`, формирование структуры `models_payload()`.
  - `viralcut/custom_debug.py`, `web/custom_debug.js`: динамическое отслеживание активной модели в режиме простоя, расширенная телеметрия загрузки с полями `pct` / `eta_s` / `download_model`, отображение `cuda_note` при нехватке библиотек CUDA DLL.
  - `tests/test_custom_features.py`: добавлен тестовый класс `CustomWhisperTest`.
- **Изменённые файлы оригинала (upstream)**:
  - `viralcut/transcribe.py`: делегирование `model_cached()` в `custom_whisper.is_model_cached()`, отслеживание имени загруженной в слот модели (`_MODEL_NAME`/`_MODEL_LONG_NAME`) для перезагрузки при смене, вызов `ensure_model_downloaded()`, поддержка аргумента `model_name`.
  - `viralcut/pipeline.py`, `viralcut/live.py`: добавлен параметр `whisper_model` в `DEFAULT_OPTIONS` / `normalize_options()`, вывод живого прогресса скачивания модели перед началом транскрипции, передача `model_name` в функцию `transcribe.transcribe()`.
  - `viralcut/main.py`: добавлено поле `whisper_model` в `/api/health`, а также эндпоинты `GET /api/whisper/models`, `POST /api/whisper/model`, `POST /api/whisper/download`.
  - `web/index.html`, `web/app.js`: монтирование контейнера `#vcWhisperSlot` в `heroHTML()`/`bindHero()`, передача `whisper_model` в `heroOptions()`, отображение активной модели в строке `#healthText`.
- **Проверка работоспособности**:
  - Запуск `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v`.

## Пайплайн сборки Windows Lite (без CUDA DLL) и инструкция для начинающих в README
- **Цель**: Настроить сборку и публикацию компактного релиза Windows x64 Lite (`ViralCut-*-windows-x64-lite.zip`) без вшитых библиотек CUDA/cuDNN и без предварительно упакованных моделей Whisper, подробно описать все ограничения Lite-сборки, а также предоставить внятную пошаговую инструкцию со ссылками как на Lite-версию, так и на архив `custom.zip` с запуском через `install_windows.bat`/`run_windows.bat` для полноценного GPU-ускорения.
- **Новые файлы**:
  - `ViralCut-lite.spec`: спецификация PyInstaller для сборки Windows Lite с исключением пакета `nvidia` и фильтрацией DLL-файлов `cublas`/`cudnn`/`cudart`/`nvrtc`, а также весов `whisper-small`, с сохранением всех подмодулей `viralcut.*`.
  - `build_windows_lite.ps1`: изолированная подготовка окружения `.venv-build` из `requirements-desktop.txt`, запуск модульных тестов, сборка через PyInstaller, валидация отсутствия библиотек CUDA и моделей, запуск headless smoke-теста готового `ViralCut.exe`, упаковка в ZIP и создание `SHA256SUMS.txt`.
  - `.github/workflows/custom-release.yml`: ручной запуск в GitHub Actions (`workflow_dispatch`), повторяющий шаги сборки Lite.
  - `release/fork-v1.4.1-1/RELEASE_NOTES.md`, `release/fork-v1.4.1-1/SHA256SUMS.txt`: заметки к выпуску и контрольные суммы для тега `fork-v1.4.1-1`.
- **Изменённые файлы**:
  - `README.md`, `CUSTOM_CHANGES.md`, `CUSTOM_CHANGES_ru.md`.
- **Проверка работоспособности**:
  - `powershell -ExecutionPolicy Bypass -File .\build_windows_lite.ps1 -Tag fork-v1.4.1-1 -Version 1.4.1-fork.1`
  - `gh release view fork-v1.4.1-1 --repo hiez1337/viralcut --json tagName,url,isLatest,assets`

## Русскоязычный журнал изменений (`CUSTOM_CHANGES_ru.md`) и включение GitHub Issues / Discussions
- **Цель**: Включить через GitHub CLI разделы Issues и Discussions в репозитории `hiez1337/viralcut`, чтобы пользователи и соавторы могли сообщать об ошибках, предлагать идеи и задавать вопросы. Предоставить полный русскоязычный дубликат журнала модификаций форка (`CUSTOM_CHANGES_ru.md`) рядом с `CUSTOM_CHANGES.md`.
- **Новые файлы**:
  - `CUSTOM_CHANGES_ru.md`: полный русскоязычный перевод и дубликат `CUSTOM_CHANGES.md`.
- **Изменённые файлы**:
  - `CUSTOM_CHANGES.md`: добавлена перекрёстная ссылка на русскоязычную версию.
  - `README.md`: добавлены прямые ссылки на русскоязычный и англоязычный журналы изменений в шапке и описании структуры репозитория.
- **Проверка работоспособности**:
  - Команда `gh repo view hiez1337/viralcut --json hasIssuesEnabled,hasDiscussionsEnabled` возвращает `true` для обоих параметров.
  - Проверка валидности перекрёстных ссылок между `README.md`, `CUSTOM_CHANGES.md` и `CUSTOM_CHANGES_ru.md`.

## Синхронизация с upstream `v1.4.2` и автоматический CI форка (`custom-ci.yml`)
- **Цель**: Объединить исправления рендера из оригинального релиза `vanwild11/viralcut` `v1.4.2` (пробная проверка аппаратного энкодера `_hw_works()` на 8 кадрах, пересборка команды `_compose()` / `_run_encode()` при откате на `libx264`, поле `"hw"` в `/api/health`, вывод причины ошибки клипа в `pipeline.py`, а также новые тесты `tests/test_render.py`) с модификациями форка и добавить автоматический CI-прогон всех тестов для PR в `custom`.
- **Новые файлы**:
  - `.github/workflows/custom-ci.yml`: запускает `python -m unittest discover -s tests -v` на `windows-latest` при каждом push и PR в ветку `custom`.
- **Изменённые / объединённые файлы**:
  - `viralcut/render.py`: принята архитектура upstream `_hw_works()`, `_video_args()`, `_run_encode()` и `_compose()` с минимальными хуками телеметрии `custom_debug` и закрытием потоков.
  - `viralcut/main.py`: объединены поле upstream `"hw": render.hw_encoder()` в `/api/health` и все кастомные эндпоинты/поля форка (`"whisper_model"`).
  - `viralcut/pipeline.py`: принят вывод причины первого упавшего клипа при `rendered == 0`.
  - `tests/test_custom_features.py`: обновлены тесты `RenderFallbackTest` под `_run_encode()` и проверку очистки процессов `custom_debug`.
  - `README.md`, `CUSTOM_CHANGES.md`, `CUSTOM_CHANGES_ru.md`: базовая версия обновлена до `1.4.2`, добавлены упоминания `custom-ci.yml` и `tests/test_render.py`.
- **Проверка работоспособности**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v` (проходят все тесты, включая новые тесты рендера `tests/test_render.py`).
  - Зелёный статус проверки `Fork CI (Tests)` в Pull Request в ветку `custom`.

## Синхронизация с upstream `v1.4.3` (плавное кадрирование, Full HD из эконом-режима и прямые ссылки)
- **Цель**: Объединить релиз `vanwild11/viralcut` `v1.4.3` (плавное кадрирование 3 fps + медиана 5 + `deadband` + сжатие пути Дугласом–Пекером против падения FFmpeg на длинных клипах, пересборка эконом-проекта в 1080p без повторного Whisper и нарезка прямых HTTP-ссылок через локальный ffmpeg) с доработками форка (`custom_downloader`, защита от гонки при повторе и скрытие окон консоли ffmpeg на Windows).
- **Изменённые / объединённые файлы**:
  - `viralcut/reframe.py`, `viralcut/db.py`, `web/app.js`, `tests/test_reframe.py`, `tests/test_render.py`: чистое слияние с сохранением кэширования `_haar_cascade()`.
  - `viralcut/downloader.py`: добавлен перехват прямых ссылок (`_extract_direct_audio` / `_cut_direct_section` с `subprocess_kwargs()` и заполнением `meta_out`) до вызова `custom_downloader.download_section_smart()`.
  - `viralcut/main.py`, `viralcut/jobs.py`, `viralcut/pipeline.py`: объединена пересборка эконом-режима в 1080p (`db.reset_clips_pending`) с безопасной сериализацией `jobs.submit_retry(..., segments_hd=...)` и передачей `meta_out`.
  - `README.md`, `CUSTOM_CHANGES.md`, `CUSTOM_CHANGES_ru.md`: базовая версия обновлена до `1.4.3`.
- **Проверка работоспособности**:
  - `.\.venv-win\Scripts\python.exe -m unittest discover -s tests -v` (включая новый 36-секундный тест рендера `LongClipRenderTest`).

<!-- Шаблон для будущих изменений:
## [Название функции / исправления]
- **Цель**: Зачем сделано изменение и ожидаемое поведение.
- **Новые файлы**: `path/to/new_file.py`
- **Изменённые файлы оригинала (upstream)**: `path/to/upstream_file.py` (минимальный хук/импорт)
- **Проверка работоспособности**: Команда или действия для проверки.
-->
