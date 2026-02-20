# RaySharp NVR — интеграция для Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/11058/OEM-RaySharp-NVR-Ai.svg)](https://github.com/11058/OEM-RaySharp-NVR-Ai/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Полнофункциональная пользовательская интеграция Home Assistant для видеорегистраторов OEM вендора **RaySharp** (HTTP API v8.2.x).
Поддерживает push-уведомления о тревогах в реальном времени, AI-аналитику, историю обнаружений, управление PTZ, снимки событий и многое другое — всё через локальный HTTP API без облака.

---

## Возможности

### Платформы

| Платформа | Сущности |
|---|---|
| **Camera** | По одной сущности на каждый онлайн-канал — RTSP-видеопоток |
| **Image** | Снимок последнего AI-события на каждом канале (JPEG, обновляется в реальном времени) |
| **Sensor** | Устройство, диски, сеть, AI-счётчики, статус EventPush, история номеров и лиц |
| **Binary Sensor** | Связь с NVR, статус каналов, потеря видео, 16 типов тревожных событий |
| **Event** | Событийные сущности NVR и каждого канала (все типы тревог) |
| **Switch** | Глобальное снятие с охраны, per-channel включение записи по тревоге |
| **Button** | Перезагрузка NVR |

### Сервисы

| Сервис | Описание |
|---|---|
| `raysharp_nvr.ptz_control` | Управление PTZ — движение, остановка, переход к пресету |
| `raysharp_nvr.get_snapshot` | Захват кадра JPEG с любого канала |
| `raysharp_nvr.trigger_alarm_output` | Активация/деактивация физических выходов тревоги |
| `raysharp_nvr.search_records` | Поиск видеозаписей по каналу, времени и типу |
| `raysharp_nvr.clear_detections_history` | Очистка истории распознанных номеров и/или лиц |

### Типы тревог (push-события)

Движение, Человек, Транспорт, Пересечение линии, Вторжение в периметр, Обнаружение лица, Распознавание номеров, IO-тревога, Стационарный объект, Обнаружение звука, Блуждание, Вход в зону, Выход из зоны, Закрытие камеры, PIR-движение, Плотность толпы.

### AI-аналитика (зависит от модели NVR)

- Счётчик и статистика обнаруженных лиц
- Счётчик распознанных номерных знаков
- Кросс-подсчёт (вход/выход) — глобально и по каналам
- Статистика людей и транспорта
- Статистика тепловых карт

### История обнаружений (номера и лица)

- Накопительные сенсоры хранят все события за **30 дней** в постоянном хранилище HA
- Для каждого номера автоматически запрашиваются: марка автомобиля, владелец, принадлежность к списку NVR (Разрешённые / Запрещённые / Незнакомец)
- Для лиц определяется группа и имя из базы NVR
- Состояние сенсора = количество событий за последние 24 часа
- Атрибуты содержат полный список с датой/временем, каналом и статусом
- Данные сохраняются между перезапусками HA
- Очистка через сервис `clear_detections_history`

---

## Требования

- Home Assistant **2024.1.0** и новее
- RaySharp NVR с **HTTP API v8.2.x** (протестировано на 8.2.4–8.2.7)
- Сетевой доступ от HA до NVR (локальная сеть)
- Учётные данные администратора NVR

---

## Установка

### Через HACS (рекомендуется)

1. Откройте **HACS → Интеграции → ⋮ → Пользовательские репозитории**
2. Добавьте URL: `https://github.com/11058/OEM-RaySharp-NVR-Ai`
   Категория: `Integration`
3. Нажмите **Загрузить**
4. Перезапустите Home Assistant

### Вручную

1. Скопируйте папку `custom_components/raysharp_nvr/` в директорию `config/custom_components/` вашего HA
2. Перезапустите Home Assistant

---

## Настройка

1. Перейдите в **Настройки → Устройства и службы → Добавить интеграцию**
2. Найдите **RaySharp NVR**
3. Введите:
   - **Host** — IP-адрес или имя хоста NVR
   - **Port** — HTTP-порт (по умолчанию: `80`)
   - **Username** — логин администратора NVR (по умолчанию: `admin`)
   - **Password** — пароль NVR

### Параметры

После настройки нажмите **Настроить** для изменения параметров:

| Параметр | По умолчанию | Описание |
|---|---|---|
| Интервал обновления | 30 с | Как часто опрашивать NVR |
| Автонастройка EventPush | Да | Автоматически настроить отправку тревог на HA |
| Таймаут сброса событий | 30 с | Через сколько секунд бинарный сенсор возвращается в OFF |

---

## EventPush — тревоги в реальном времени

Интеграция регистрирует **webhook** в Home Assistant и может автоматически настроить NVR для отправки тревожных событий на него. Это обеспечивает мгновенную (< 1 с) реакцию на движение, обнаружение людей и другие тревоги без опроса.

При ручной настройке укажите в веб-интерфейсе NVR:
- **Адрес**: IP-адрес вашего HA
- **Порт**: `8123` (или ваш порт HA)
- **URL**: `api/webhook/raysharp_nvr_<entry_id>`

> `entry_id` отображается в логах HA при старте интеграции (уровень INFO).

---

## Сервисы

### `raysharp_nvr.ptz_control`

```yaml
service: raysharp_nvr.ptz_control
data:
  config_entry_id: "your_entry_id"
  channel: 1
  command: "Ptz_Cmd_Up"
  state: "Start"
  speed: 50
```

Доступные команды: `Ptz_Cmd_Up`, `Ptz_Cmd_Down`, `Ptz_Cmd_Left`, `Ptz_Cmd_Right`,
`Ptz_Cmd_UpLeft`, `Ptz_Cmd_UpRight`, `Ptz_Cmd_DownLeft`, `Ptz_Cmd_DownRight`,
`Ptz_Cmd_ZoomAdd`, `Ptz_Cmd_ZoomDec`, `Ptz_Cmd_FocusAdd`, `Ptz_Cmd_FocusDec`,
`Ptz_Cmd_Stop`, `Ptz_Cmd_GotoPreset`, `Ptz_Cmd_SetPreset`, `Ptz_Btn_AutoFocus`

### `raysharp_nvr.get_snapshot`

```yaml
service: raysharp_nvr.get_snapshot
data:
  config_entry_id: "your_entry_id"
  channel: 1
```

Генерирует событие `raysharp_nvr_snapshot` с данными кадра в формате base64 JPEG.

### `raysharp_nvr.trigger_alarm_output`

```yaml
service: raysharp_nvr.trigger_alarm_output
data:
  config_entry_id: "your_entry_id"
  output_id: "Local->1"
  active: true
```

### `raysharp_nvr.search_records`

```yaml
service: raysharp_nvr.search_records
data:
  config_entry_id: "your_entry_id"
  channel: 1
  start_time: "2024-01-01T00:00:00"
  end_time: "2024-01-01T23:59:59"
  record_type: "motion"
```

Генерирует событие `raysharp_nvr_record_search_result` с результатами поиска.

### `raysharp_nvr.clear_detections_history`

```yaml
service: raysharp_nvr.clear_detections_history
data:
  config_entry_id: "your_entry_id"
  detection_type: "all"   # "plates" | "faces" | "all"
```

Полностью очищает сохранённую историю распознаваний из постоянного хранилища.

---

## Карточки для Dashboard

### Карточка: Номерные знаки (последние 24 часа)

```yaml
type: markdown
title: Номерные знаки (24 часа)
content: |
  {% set rows = state_attr(
    'sensor.YOUR_NVR_plates_detected_today',
    'plates') | default([]) %}
  {% if rows | count > 0 %}
  {% for p in rows | sort(
    attribute='timestamp', reverse=true) %}
  **{{ p.plate_number }}** — {{ p.car_brand or '' }}
  {{ p.time }} · CH{{ p.channel }}
  *{{ p.list_type_label or 'Неизвестные' }}*
  ---
  {% endfor %}
  {% else %}
  *Нет событий за 24 часа*
  {% endif %}
```

### Карточка: Лица (последние 24 часа)

```yaml
type: markdown
title: Лица (24 часа)
content: |
  {% set rows = state_attr(
    'sensor.YOUR_NVR_faces_detected_today',
    'detections') | default([]) %}
  {% if rows | count > 0 %}
  {% for f in rows | sort(
    attribute='timestamp', reverse=true) %}
  **{{ f.face_name or 'Незнакомец' }}**
  {{ f.time }} · CH{{ f.channel }}
  *{{ f.list_type_label or '—' }}*
  ---
  {% endfor %}
  {% else %}
  *Нет событий за 24 часа*
  {% endif %}
```

> Замените `sensor.YOUR_NVR_...` на реальный entity_id своих сенсоров (найти в **Settings → Entities**, поиск "plates" или "faces").

---

## Примеры автоматизаций

### Уведомление при обнаружении человека

```yaml
automation:
  trigger:
    platform: state
    entity_id: binary_sensor.ch1_cam01_person_detected
    to: "on"
  action:
    service: notify.mobile_app
    data:
      message: "Обнаружен человек на камере 1!"
```

### PTZ-патруль при уходе из дома

```yaml
automation:
  trigger:
    platform: state
    entity_id: alarm_control_panel.home
    to: "away"
  action:
    service: raysharp_nvr.ptz_control
    data:
      config_entry_id: "your_entry_id"
      channel: 1
      command: "Ptz_Cmd_GotoPreset"
      preset_num: 1
```

### Уведомление при обнаружении запрещённого номера

```yaml
automation:
  trigger:
    platform: event
    event_type: raysharp_nvr_snapshot
    event_data:
      alarm_type: plate
  condition:
    condition: template
    value_template: >
      {{ trigger.event.data.get('list_type') == 'blocked' }}
  action:
    service: notify.mobile_app
    data:
      message: >
        Запрещённый номер:
        {{ trigger.event.data.get('plate_number') }}
        на CH{{ trigger.event.data.get('channel') }}
```

### Постановка/снятие с охраны по присутствию

```yaml
automation:
  - trigger:
      platform: state
      entity_id: person.resident
      to: "not_home"
    action:
      service: switch.turn_off
      entity_id: switch.raysharp_nvr_alarm_disarming

  - trigger:
      platform: state
      entity_id: person.resident
      to: "home"
    action:
      service: switch.turn_on
      entity_id: switch.raysharp_nvr_alarm_disarming
```

---

## Покрытие API

Интеграция реализует **RaySharp HTTP API v8.2.7**, включая:

- Аутентификация и управление сессией (HTTP Digest auth, CSRF, heartbeat)
- Информация об устройстве, каналах, системе
- Мониторинг хранилища / дисков (ёмкость, свободное место)
- Получение RTSP-ссылок для стриминга (основной и субпоток)
- Конфигурация всех типов тревог: motion, IO, exception, face, line-crossing, perimeter, SOD, PIR, sound, occlusion, pedestrian
- Глобальное снятие с охраны (disarming)
- EventPush — HTTP-push тревог на webhook HA с автонастройкой
- AI-аналитика: лица, номерные знаки, кросс-подсчёт, тепловые карты, статистика объектов
- База данных номерных знаков (AddedPlates) и групп лиц (FDGroup) для обогащения событий
- Управление PTZ (движение, zoom, focus, пресеты)
- Ручное управление выходами тревоги
- Поиск видеозаписей
- Обслуживание: перезагрузка NVR

---

## Решение проблем

**Нет подключения** — проверьте IP и порт NVR, убедитесь что HTTP API включён в настройках NVR.

**Ошибка аутентификации** — проверьте логин и пароль от веб-интерфейса NVR.

**Нет push-событий** — убедитесь, что HA доступен с NVR по сети. Включите автонастройку EventPush в параметрах интеграции или настройте вручную в веб-интерфейсе NVR.

**AI-сущности не появляются** — ваша модель NVR может не поддерживать AI-функции. Сущности создаются только когда соответствующие API-эндпоинты отвечают успешно.

**PTZ не работает** — убедитесь, что к каналу подключена PTZ-камера и PTZ включён в настройках канала NVR.

**История номеров/лиц пустая** — убедитесь что EventPush работает (см. выше). История заполняется только из real-time событий, не из истории NVR.

---

## Участие в разработке

Issues и pull requests приветствуются на [GitHub](https://github.com/11058/OEM-RaySharp-NVR-Ai).

---

## Лицензия

MIT License — см. [LICENSE](LICENSE).
