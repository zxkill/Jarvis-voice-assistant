#pragma once

#include <stdint.h>
#include <string>
#include <vector>

#include "audio_capture.h"

/**
 * \file remote_control.h
 * \brief Интерфейс удалённого управления роботом через встроенный веб-сервер.
 */

namespace RemoteControl {

/**
 * \brief Типы действий, которые может запросить оператор.
 */
enum class Action : uint8_t {
  Move,       ///< Линейное движение (вперёд/назад)
  Rotate,     ///< Разворот на фиксированный угол
  EmergencyStop ///< Немедленная остановка всех приводов
};

/**
 * \brief Направление для линейного движения или разворота.
 */
enum class Direction : uint8_t {
  Forward,   ///< Движение вперёд или поворот влево
  Backward   ///< Движение назад или поворот вправо
};

/**
 * \brief Команда, поступившая от удалённого клиента.
 */
struct Command {
  Action action = Action::Move; ///< Тип действия.
  Direction direction = Direction::Forward; ///< Направление движения.
  float value = 0.0f; ///< Метры для движения или градусы для поворота.
  int duty = 0; ///< Базовое значение duty цикла (0 если использовать значение по умолчанию).
};

/**
 * \brief Диагностическая сводка, которую отображает веб-интерфейс.
 */
struct Diagnostics {
  float busVoltage = 0.0f; ///< Напряжение шины питания, В.
  float currentA = 0.0f;   ///< Ток потребления, А.
  float powerW = 0.0f;     ///< Расход мощности, Вт.
  float batteryPercent = 0.0f; ///< Оценочный уровень заряда, %.
  float headingDeg = 0.0f; ///< Текущий курс робота, градусы.
  float turnRateDps = 0.0f; ///< Текущая угловая скорость, град/с.
  float gyroBiasDps = 0.0f; ///< Смещение гироскопа, град/с.
  long ticksLeft = 0;      ///< Накопленные тики левого энкодера.
  long ticksRight = 0;     ///< Накопленные тики правого энкодера.
  float velocityLeft = 0.0f; ///< Скорость левого колеса, м/с.
  float velocityRight = 0.0f; ///< Скорость правого колеса, м/с.
  float distanceLeft = 0.0f; ///< Пройденное левым колесом расстояние, м.
  float distanceRight = 0.0f; ///< Пройденное правым колесом расстояние, м.
  float temperatureC = 0.0f; ///< Температура с MPU, °C.
  bool audioLocalizationActive = false; ///< Локальная ли оценка направления.
  float audioDirectionDeg = 0.0f; ///< Оценка направления звука, °.
  float audioConfidence = 0.0f;   ///< Уверенность алгоритма определения источника.
  float audioRmsLeft = 0.0f;      ///< RMS уровень левого канала (0..1).
  float audioRmsRight = 0.0f;     ///< RMS уровень правого канала (0..1).
  uint32_t audioSampleRate = 0;   ///< Частота дискретизации аудиопотока, Гц.
  uint32_t audioFrameSamples = 0; ///< Количество сэмплов в кадре на канал.
  bool audioStreamReady = false;  ///< Признак наличия свежего аудиокадра для сервера.
  float audioMicSpacingMeters = 0.0f; ///< База микрофонов, м.
};

/**
 * \brief Параметры подключения для потоковой передачи аудио на сервер.
 */
struct AudioStreamConfig {
  std::string endpoint;             ///< WebSocket-адрес сервера (ws:// или wss://).
  std::string authHeader;           ///< Дополнительный заголовок Authorization (опционально).
  std::string subprotocol;          ///< Имя WebSocket-подпротокола, если сервер его требует.
  uint32_t handshakeTimeoutMs = 5000;   ///< Максимальная длительность рукопожатия перед попыткой переподключения.
  uint32_t reconnectIntervalMs = 3000;  ///< Интервал между автоматическими переподключениями, мс.
  uint32_t pingIntervalMs = 15000;      ///< Частота отправки ping для поддержания соединения, мс.
  bool xiaoZhiCompat = true;            ///< Включает проводной протокол XiaoZhi (hello + BinaryProtocol2/3).
  uint16_t xiaoZhiVersion = 3;          ///< Версия бинарного протокола XiaoZhi (2 или 3).
  uint16_t xiaoZhiFrameDurationMs = 60; ///< Длительность кадра, используемая в hello.
  uint32_t xiaoZhiSampleRate = 44100;   ///< Частота дискретизации для приветствия и расчёта размеров кадра.
  uint16_t xiaoZhiChannels = 1;         ///< Количество каналов (xiaozhi использует моно).
  std::string xiaoZhiFormat = "opus";   ///< Кодек, объявляемый в hello (opus или pcm16 для отладки).
};

/**
 * \brief Статистика потоковой передачи аудио.
 */
struct AudioStreamStats {
  uint32_t framesSent = 0;             ///< Количество успешно отправленных бинарных кадров.
  uint32_t framesFailed = 0;           ///< Сколько кадров не удалось передать через WebSocket.
  uint32_t nextSequence = 1;           ///< Последовательный номер следующего кадра.
  uint64_t lastAttemptMs = 0;          ///< Время последней попытки отправки (millis).
  uint32_t lastDurationMs = 0;         ///< Длительность последней отправки, мс.
  bool lastAttemptOk = false;          ///< Итог последней отправки.
  std::string lastError;               ///< Человекочитаемое описание последней ошибки.
  uint64_t bytesSent = 0;              ///< Суммарный объём переданных данных, байт.
  uint32_t queueDepth = 0;             ///< Текущая заполненность очереди фоновой отправки.
  uint32_t queueHighWatermark = 0;     ///< Максимальная достигнутая глубина очереди.
  uint32_t queueDrops = 0;             ///< Сколько кадров пришлось отбросить из-за переполнения очереди.
  uint32_t queueStalls = 0;            ///< Сколько раз поток ожидал освобождение очереди.
  uint32_t wsOfflineDrops = 0;         ///< Сколько кадров отброшено из-за отсутствия WebSocket-соединения.
  uint32_t wsReconnects = 0;           ///< Количество попыток переподключения к WebSocket.
  uint32_t wsTimeouts = 0;             ///< Сколько раз рукопожатие WebSocket превысило таймаут и было принудительно прервано.
  uint64_t wsLastConnectMs = 0;        ///< Время последнего успешного подключения.
  uint64_t wsLastDisconnectMs = 0;     ///< Время последнего разрыва соединения.
  bool wsConnected = false;            ///< Текущее состояние WebSocket-соединения.
};

/**
 * \brief Диагностика веб-сокета телеметрии и потока статуса.
 */
struct TelemetryStreamStats {
  uint32_t clientsConnected = 0;   ///< Сколько браузеров/клиентов сейчас подписаны на телеметрию.
  uint32_t clientsMax = 0;         ///< Максимальное одновременное количество клиентов с момента запуска.
  uint32_t connectEvents = 0;      ///< Сколько успешных подключений произошло.
  uint32_t disconnectEvents = 0;   ///< Сколько раз клиенты отключались.
  uint32_t lastClientId = 0;       ///< Идентификатор клиента, инициировавшего последнее событие.
  uint64_t lastEventMs = 0;        ///< Момент времени последнего события (подключение, отправка, ошибка) в миллисекундах.
  uint64_t messagesSent = 0;       ///< Общее количество отправленных сообщений статуса.
  uint64_t bytesSent = 0;          ///< Совокупный объём данных статуса, переданный через WebSocket, байт.
  uint32_t lastPayloadBytes = 0;   ///< Размер последнего отправленного JSON-пакета, байт.
  uint64_t duplicatesSkipped = 0;  ///< Сколько раз обновление не отправлялось из-за отсутствия изменений.
  uint64_t lastBroadcastMs = 0;    ///< Момент времени последней успешной отправки статуса (millis()).
  std::string lastError;           ///< Последнее текстовое описание ошибки телеметрии.
};

#ifndef ARDUINO
std::vector<uint8_t> build_audio_stream_frame(const AudioStreamConfig& cfg,
                                              const Audio::PcmChunk& chunk,
                                              const Audio::Diagnostics& diag,
                                              uint32_t sequence,
                                              uint16_t serverFrameDurationMs,
                                              uint32_t serverSampleRate,
                                              uint16_t serverChannels);
#endif

namespace detail {
/**
 * \brief Проверка превышения таймаута рукопожатия WebSocket.
 * \param timeoutMs Конфигурируемый таймаут, мс.
 * \param elapsedMs Сколько миллисекунд прошло с момента старта рукопожатия.
 * \return true, если elapsedMs строго больше timeoutMs и следует перезапустить соединение.
 */
bool handshake_timeout_elapsed(uint32_t timeoutMs, uint32_t elapsedMs);

/**
 * \brief Формирует человеко-читаемое описание статуса аудиопотока.
 *
 * Возвращает массив коротких строк, каждая из которых является отдельной
 * меткой для карточки «Поток аудио» в веб-интерфейсе. Такая структура упрощает
 * тестирование форматирования и даёт возможность UI переносить строки без
 * потери информации.
 */
std::vector<std::string> build_audio_stream_summary(const AudioStreamConfig& cfg,
                                                    const AudioStreamStats& stats,
                                                    const Diagnostics& diag);
} // namespace detail

/**
 * \brief Инициализация Wi-Fi и веб-сервера удалённого управления.
 * \param ssid     Название точки доступа.
 * \param password Пароль к точке доступа.
 */
void init(const char* ssid, const char* password);

/**
 * \brief Периодический вызов из loop() для обслуживания HTTP-клиентов.
 */
void loop();

/**
 * \brief Обновление диагностических данных для веб-интерфейса.
 * \param diag Новая структура с измерениями.
 */
void update_diagnostics(const Diagnostics& diag);

/**
 * \brief Помещает новую команду в очередь, если в ней нет активного задания.
 * \param cmd Команда, сформированная веб-интерфейсом или тестом.
 * \return true, если команда добавлена; false, если очередь занята.
 */
bool push_command(const Command& cmd);

/**
 * \brief Извлекает команду из очереди для исполнения движением.
 * \param out Записывает извлечённую команду.
 * \return true, если команда была; false, если очередь пуста.
 */
bool fetch_command(Command& out);

/**
 * \brief Принудительно очищает очередь команд (используется при аварийной остановке).
 */
void clear_commands();

/**
 * \brief Отмечает завершение текущего движения, чтобы разблокировать очередь и телеметрию.
 */
void notify_command_complete();

/**
 * \brief Возвращает true, если удалённый контроллер занят исполнением движения.
 */
bool is_busy();

/**
 * \brief Разрешены ли обновления телеметрии.
 *
 * После перехода на асинхронное выполнение команд функция всегда возвращает
 * true, но оставлена для обратной совместимости с существующими вызовами.
 */
bool telemetry_updates_allowed();

/**
 * \brief Настраивает параметры потоковой отправки аудио на сервер.
 */
void set_audio_stream_config(const AudioStreamConfig& cfg);

/**
 * \brief Возвращает текущую конфигурацию потокового аудио.
 */
AudioStreamConfig audio_stream_config();

/**
 * \brief Возвращает актуальную статистику передачи аудио.
 */
AudioStreamStats audio_stream_stats();

/**
 * \brief Возвращает статистику WebSocket-канала телеметрии.
 */
TelemetryStreamStats telemetry_stream_stats();

} // namespace RemoteControl
