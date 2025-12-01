#include "remote_control.h"
#include "config_store.h"
#include "audio_capture.h"
#include "audio_playback.h"

#ifdef ARDUINO
#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <WebSocketsClient.h>
#include <WebSocketsServer.h>
#include <ArduinoJson.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#endif

#include <ctype.h>
#include <math.h>
#include <stdlib.h>
#include <algorithm>
#include <new>
#include <utility>
#include <string>
#include <vector>
#include <stdio.h>
#include <cstring>

namespace RemoteControl {

namespace detail {
bool handshake_timeout_elapsed(uint32_t timeoutMs, uint32_t elapsedMs) {
  // Таймаут «0» трактуем как «ожидать бесконечно», что удобно для отладки.
  if (timeoutMs == 0) {
    return false;
  }
  return elapsedMs > timeoutMs;
}

std::vector<std::string> build_audio_stream_summary(const AudioStreamConfig& cfg,
                                                    const AudioStreamStats& stats,
                                                    const Diagnostics& diag) {
  // Используем понятные операторы сложения строк, чтобы итоговые подписи легко читались и в логах, и в UI.
  std::vector<std::string> parts;
  parts.reserve(10);

  if (cfg.endpoint.empty()) {
    // Если endpoint не задан, карточка явно показывает отключение сервиса, чтобы оператор не искал причину.
    parts.emplace_back("отключено");
    return parts;
  }

  parts.emplace_back(diag.audioStreamReady ? "готов" : "ожидание");
  parts.emplace_back("ok:" + std::to_string(stats.framesSent) + " err:" + std::to_string(stats.framesFailed));

  // Выводим глубину очереди: даже нулевые значения полезны, потому что подтверждают стабильность канала.
  parts.emplace_back("queue:" + std::to_string(stats.queueDepth) + "/" + std::to_string(stats.queueHighWatermark));

  if (stats.queueDrops > 0) {
    parts.emplace_back("drop:" + std::to_string(stats.queueDrops));
  }
  if (stats.wsOfflineDrops > 0) {
    parts.emplace_back("offline:" + std::to_string(stats.wsOfflineDrops));
  }
  if (stats.queueStalls > 0) {
    parts.emplace_back("stall:" + std::to_string(stats.queueStalls));
  }

  parts.emplace_back(stats.wsConnected ? "ws:on" : "ws:off");
  parts.emplace_back("recon:" + std::to_string(stats.wsReconnects));

  if (stats.wsTimeouts > 0) {
    parts.emplace_back("timeout:" + std::to_string(stats.wsTimeouts));
  }

  parts.emplace_back("bytes:" + std::to_string(stats.bytesSent));

  // Даже нулевое время попытки полезно для аудита — значит, отправка ещё не выполнялась.
  parts.emplace_back(std::to_string(stats.lastDurationMs) + "мс");

  if (!stats.lastError.empty()) {
    parts.push_back(stats.lastError);
  }

  // Endpoint выводим последним — так оператор сразу видит целевой адрес сервиса распознавания речи.
  parts.push_back(cfg.endpoint);

  return parts;
}
} // namespace detail

std::vector<uint8_t> build_audio_stream_frame(const Audio::PcmChunk& chunk,
                                              const Audio::Diagnostics& diag,
                                              uint32_t sequence);

namespace {
  // --- Общие (кроссплатформенные) данные ---
  Diagnostics gDiagnostics{};            ///< Последние диагностические данные.
  Command gPendingCommand{};             ///< Ожидающая выполнения команда.
  bool gHasPendingCommand = false;       ///< Флаг наличия команды в очереди.
  bool gCommandInProgress = false;       ///< Истина, если движение уже выполняется и блокирует очередь.

  AudioStreamConfig gAudioStreamConfig{}; ///< Текущая конфигурация потоковой отправки аудио.
  AudioStreamStats gAudioStreamStats{};   ///< Статистика отправленных/ошибочных кадров.
  TelemetryStreamStats gTelemetryStats{}; ///< Диагностика канала телеметрии для веб-интерфейса.

  bool gMicPausedForPlayback = false;     ///< Флаг, что микрофон временно не отправляет данные во время TTS.

  bool gStatusDirty = true;               ///< Требуется ли отправить свежий JSON статуса по WebSocket.
  std::string gLastStatusJson;            ///< Кэш последнего JSON статуса для мгновенной выдачи новым подписчикам.
  unsigned long gLastTelemetryBroadcastMs = 0; ///< Время последней рассылки статуса по WebSocket.
  unsigned long gLastTelemetryLogMs = 0;        ///< Таймер периодического логирования состояния телеметрии.
  unsigned long gLastWsWaitLogMs = 0;            ///< Таймер логирования ожидания подключения WebSocket.
  unsigned long gLastMicPauseLogMs = 0;           ///< Таймер логирования паузы микрофона во время воспроизведения.

#ifdef ARDUINO
  constexpr size_t AUDIO_STREAM_QUEUE_DEPTH = 6;      ///< Глубина очереди фоновой отправки аудио.
  constexpr uint32_t AUDIO_STREAM_TASK_STACK = 8192;  ///< Размер стека задачи отправки (байт).
  constexpr uint32_t AUDIO_STREAM_TASK_STACK_WORDS = AUDIO_STREAM_TASK_STACK / sizeof(StackType_t);
  constexpr UBaseType_t AUDIO_STREAM_TASK_PRIORITY = 2; ///< Приоритет задачи отправки аудио.

  constexpr uint16_t TELEMETRY_WS_PORT = 81;                 ///< Порт WebSocket для телеметрии браузера/Home Assistant.
  constexpr uint32_t TELEMETRY_WS_HEARTBEAT_INTERVAL_MS = 15000; ///< Частота ping/pong для удержания соединения.
  constexpr uint32_t TELEMETRY_WS_HEARTBEAT_TIMEOUT_MS  = 6000;  ///< Таймаут ответа клиента на ping.
  constexpr uint8_t TELEMETRY_WS_HEARTBEAT_FAILURES     = 2;     ///< Допустимое число пропущенных pong до разрыва.
  constexpr uint32_t TELEMETRY_BROADCAST_MIN_INTERVAL_MS = 120;  ///< Минимальный интервал между рассылками, чтобы не засыпать сеть.

  WebSocketsServer gTelemetryWs(TELEMETRY_WS_PORT); ///< Локальный WebSocket-сервер телеметрии.
  bool gTelemetryWsStarted = false;                 ///< Инициализирован ли WebSocket-сервер.

  struct StreamWorkItem {
    std::vector<uint8_t> payload;    ///< Сериализованный бинарный кадр для WebSocket.
    Audio::Diagnostics diag;         ///< Диагностические метрики кадра (для логов и телеметрии).
    uint32_t sequence = 0;           ///< Последовательный номер кадра.
    unsigned long enqueueMs = 0;     ///< Время добавления в очередь (millis).
    size_t pcmBytes = 0;             ///< Сколько байт PCM занимает полезная нагрузка (без заголовка).
  };

  QueueHandle_t gStreamQueue = nullptr;   ///< Очередь для фоновой задачи передачи.
  TaskHandle_t gStreamTask = nullptr;     ///< Хэндл фоновой задачи передачи.
  unsigned long gLastQueueSaturationLogMs = 0; ///< Время последнего предупреждения о переполнении очереди.

  WebSocketsClient gWebsocketClient;      ///< Клиент WebSocket для потоковой передачи.
  bool gWebsocketConfigured = false;      ///< Успели ли мы сконфигурировать клиента под текущий endpoint.
  unsigned long gLastHandshakeMs = 0;     ///< Время последней попытки рукопожатия.
  std::string gLastWsUrl;                 ///< Кеш последнего endpoint для отслеживания изменений.
  std::string gWsExtraHeaders;            ///< Буфер с дополнительными HTTP-заголовками для рукопожатия.
  AudioStreamConfig gActiveWsConfig;      ///< Последняя применённая конфигурация потока.
  bool gHasActiveWsConfig = false;        ///< Флаг наличия валидной конфигурации в gActiveWsConfig.

  struct WsEndpointParts {
    std::string host;    ///< Имя хоста из URL.
    uint16_t port = 0;   ///< Порт подключения.
    std::string path;    ///< Путь (всегда начинается с '/').
    bool secure = false; ///< Используется ли TLS.
  };

  portMUX_TYPE gStreamConfigMux = portMUX_INITIALIZER_UNLOCKED; ///< Блокировка конфигурации потока.
  portMUX_TYPE gStreamStatsMux = portMUX_INITIALIZER_UNLOCKED;  ///< Блокировка статистики потока.
  portMUX_TYPE gTelemetryStatsMux = portMUX_INITIALIZER_UNLOCKED; ///< Блокировка статистики телеметрийного WebSocket.
  portMUX_TYPE gStatusDirtyMux   = portMUX_INITIALIZER_UNLOCKED;  ///< Блокировка флага gStatusDirty для многопоточности.

  inline void lock_stream_config() { taskENTER_CRITICAL(&gStreamConfigMux); }
  inline void unlock_stream_config() { taskEXIT_CRITICAL(&gStreamConfigMux); }
  inline void lock_stream_stats() { taskENTER_CRITICAL(&gStreamStatsMux); }
  inline void unlock_stream_stats() { taskEXIT_CRITICAL(&gStreamStatsMux); }
  inline void lock_telemetry_stats() { taskENTER_CRITICAL(&gTelemetryStatsMux); }
  inline void unlock_telemetry_stats() { taskEXIT_CRITICAL(&gTelemetryStatsMux); }
  inline void lock_status_dirty() { taskENTER_CRITICAL(&gStatusDirtyMux); }
  inline void unlock_status_dirty() { taskEXIT_CRITICAL(&gStatusDirtyMux); }
#else
  inline void lock_stream_config() {}
  inline void unlock_stream_config() {}
  inline void lock_stream_stats() {}
  inline void unlock_stream_stats() {}
  inline void lock_telemetry_stats() {}
  inline void unlock_telemetry_stats() {}
  inline void lock_status_dirty() {}
  inline void unlock_status_dirty() {}
#endif

  AudioStreamConfig snapshot_audio_stream_config() {
    AudioStreamConfig copy;
    lock_stream_config();
    copy = gAudioStreamConfig;
    unlock_stream_config();
    return copy;
  }

  AudioStreamStats snapshot_audio_stream_stats() {
    AudioStreamStats copy;
    lock_stream_stats();
    copy = gAudioStreamStats;
    unlock_stream_stats();
    return copy;
  }

  TelemetryStreamStats snapshot_telemetry_stats() {
    TelemetryStreamStats copy;
    lock_telemetry_stats();
    copy = gTelemetryStats;
    unlock_telemetry_stats();
    return copy;
  }

  void overwrite_audio_stream_stats(const AudioStreamStats& stats) {
    lock_stream_stats();
    gAudioStreamStats = stats;
    unlock_stream_stats();
  }

  void mark_status_dirty() {
    lock_status_dirty();
    gStatusDirty = true;
    unlock_status_dirty();
  }

  void update_queue_depth_metric() {
#ifdef ARDUINO
    if (!gStreamQueue) {
      return;
    }
    const UBaseType_t depth = uxQueueMessagesWaiting(gStreamQueue);
    lock_stream_stats();
    gAudioStreamStats.queueDepth = static_cast<uint32_t>(depth);
    gAudioStreamStats.queueHighWatermark = std::max(gAudioStreamStats.queueHighWatermark,
                                                   gAudioStreamStats.queueDepth);
    unlock_stream_stats();
    // Отмечаем необходимость обновления статуса, чтобы клиенты мгновенно увидели глубину очереди.
    mark_status_dirty();
#endif
  }

  void pause_mic_stream(const char* reason) {
    if (gMicPausedForPlayback) {
      return;
    }
    gMicPausedForPlayback = true;
#ifdef ARDUINO
    Serial.printf("[AUDIO] поток микрофона приостановлен: %s\n", reason ? reason : "не указано");
#endif
  }

  void resume_mic_stream(const char* reason) {
    if (!gMicPausedForPlayback) {
      return;
    }
    gMicPausedForPlayback = false;
#ifdef ARDUINO
    Serial.printf("[AUDIO] поток микрофона возобновлён: %s\n", reason ? reason : "не указано");
#endif
  }

  bool consume_status_dirty_flag() {
    lock_status_dirty();
    const bool dirty = gStatusDirty;
    gStatusDirty = false;
    unlock_status_dirty();
    return dirty;
  }

  void telemetry_stats_note_send(size_t bytes, unsigned long nowMs) {
    lock_telemetry_stats();
    gTelemetryStats.messagesSent++;
    gTelemetryStats.bytesSent += static_cast<uint64_t>(bytes);
    gTelemetryStats.lastPayloadBytes = static_cast<uint32_t>(bytes);
    gTelemetryStats.lastEventMs = nowMs;
    gTelemetryStats.lastBroadcastMs = nowMs;
    gTelemetryStats.lastError.clear();
    unlock_telemetry_stats();
  }

  void telemetry_stats_note_duplicate(unsigned long nowMs) {
    lock_telemetry_stats();
    gTelemetryStats.duplicatesSkipped++;
    gTelemetryStats.lastEventMs = nowMs;
    unlock_telemetry_stats();
  }

  void telemetry_stats_on_connect(uint8_t clientId, unsigned long nowMs) {
    lock_telemetry_stats();
    gTelemetryStats.clientsConnected++;
    gTelemetryStats.connectEvents++;
    gTelemetryStats.clientsMax = std::max(gTelemetryStats.clientsMax, gTelemetryStats.clientsConnected);
    gTelemetryStats.lastClientId = clientId;
    gTelemetryStats.lastEventMs = nowMs;
    gTelemetryStats.lastError.clear();
    unlock_telemetry_stats();
    mark_status_dirty();
  }

  void telemetry_stats_on_disconnect(uint8_t clientId, unsigned long nowMs) {
    lock_telemetry_stats();
    if (gTelemetryStats.clientsConnected > 0) {
      gTelemetryStats.clientsConnected--;
    }
    gTelemetryStats.disconnectEvents++;
    gTelemetryStats.lastClientId = clientId;
    gTelemetryStats.lastEventMs = nowMs;
    unlock_telemetry_stats();
    mark_status_dirty();
  }

  void telemetry_stats_note_error(const char* message, unsigned long nowMs) {
    lock_telemetry_stats();
    gTelemetryStats.lastError = message ? message : "unknown";
    gTelemetryStats.lastEventMs = nowMs;
    unlock_telemetry_stats();
    mark_status_dirty();
  }

#ifdef ARDUINO

  void telemetry_stats_touch(uint8_t clientId, unsigned long nowMs) {
    lock_telemetry_stats();
    gTelemetryStats.lastClientId = clientId;
    gTelemetryStats.lastEventMs = nowMs;
    unlock_telemetry_stats();
  }

  // Предварительное объявление генератора JSON статуса, чтобы использовать его до определения.
  String render_status_json();

  void telemetry_ws_send_cached_to(uint8_t clientId, unsigned long nowMs) {
    if (gLastStatusJson.empty()) {
      return;
    }
    gTelemetryWs.sendTXT(clientId,
                         reinterpret_cast<const uint8_t*>(gLastStatusJson.c_str()),
                         gLastStatusJson.size());
    telemetry_stats_note_send(gLastStatusJson.size(), nowMs);
    gLastTelemetryBroadcastMs = nowMs;
    Serial.printf("[TELEM] кеш статуса (%u байт) отправлен клиенту #%u\n",
                  static_cast<unsigned>(gLastStatusJson.size()),
                  static_cast<unsigned>(clientId));
  }

  void emit_status_over_ws(bool force) {
    if (!gTelemetryWsStarted) {
      return;
    }

    const unsigned long nowMs = millis();
    if (!force && (nowMs - gLastTelemetryBroadcastMs) < TELEMETRY_BROADCAST_MIN_INTERVAL_MS) {
      return;
    }

    bool shouldSend = force;
    const bool pendingDirty = consume_status_dirty_flag();
    shouldSend = shouldSend || pendingDirty;
    if (!shouldSend) {
      return;
    }

    const String jsonArduino = render_status_json();
    std::string payload(jsonArduino.c_str(), jsonArduino.length());
    const bool changed = (payload != gLastStatusJson);
    if (changed) {
      gLastStatusJson = payload;
    }

    if (!force && !changed) {
      telemetry_stats_note_duplicate(nowMs);
      return;
    }

    if (gLastStatusJson.empty()) {
      telemetry_stats_note_error("empty-json", nowMs);
      return;
    }

    const auto statsSnapshot = snapshot_telemetry_stats();
    if (statsSnapshot.clientsConnected == 0) {
      telemetry_stats_note_duplicate(nowMs);
      gLastTelemetryBroadcastMs = nowMs;
      return;
    }

    gTelemetryWs.broadcastTXT(reinterpret_cast<const uint8_t*>(gLastStatusJson.c_str()),
                              gLastStatusJson.size());
    telemetry_stats_note_send(gLastStatusJson.size(), nowMs);
    gLastTelemetryBroadcastMs = nowMs;

    if (nowMs - gLastTelemetryLogMs > 5000UL) {
      Serial.printf("[TELEM] статус %u байт отправлен %u подписчикам\n",
                    static_cast<unsigned>(gLastStatusJson.size()),
                    static_cast<unsigned>(statsSnapshot.clientsConnected));
      gLastTelemetryLogMs = nowMs;
    }
  }

  void telemetry_ws_event(uint8_t clientId, WStype_t type, uint8_t* payload, size_t length) {
    const unsigned long nowMs = millis();
    switch (type) {
      case WStype_CONNECTED: {
        telemetry_stats_on_connect(clientId, nowMs);
        const char* path = payload ? reinterpret_cast<const char*>(payload) : "/";
        IPAddress ip = gTelemetryWs.remoteIP(clientId);
        Serial.printf("[TELEM] клиент #%u подключился (%s), путь %s\n",
                      static_cast<unsigned>(clientId),
                      ip.toString().c_str(),
                      path);
        if (!gLastStatusJson.empty()) {
          telemetry_ws_send_cached_to(clientId, nowMs);
        } else {
          mark_status_dirty();
          emit_status_over_ws(true);
        }
        break;
      }
      case WStype_DISCONNECTED: {
        telemetry_stats_on_disconnect(clientId, nowMs);
        Serial.printf("[TELEM] клиент #%u отключился\n", static_cast<unsigned>(clientId));
        break;
      }
      case WStype_TEXT: {
        std::string msg;
        if (payload && length > 0) {
          msg.assign(reinterpret_cast<const char*>(payload), length);
        }
        Serial.printf("[TELEM] текст от #%u: %s\n",
                      static_cast<unsigned>(clientId),
                      msg.empty() ? "(пусто)" : msg.c_str());
        mark_status_dirty();
        emit_status_over_ws(true);
        break;
      }
      case WStype_BIN: {
        Serial.printf("[TELEM] бинарное сообщение от #%u (%u байт) проигнорировано\n",
                      static_cast<unsigned>(clientId),
                      static_cast<unsigned>(length));
        telemetry_stats_note_error("unexpected-binary", nowMs);
        break;
      }
      case WStype_ERROR: {
        telemetry_stats_note_error("ws-error", nowMs);
        Serial.printf("[TELEM] ошибка WebSocket от клиента #%u\n", static_cast<unsigned>(clientId));
        break;
      }
      case WStype_PING:
      case WStype_PONG:
      case WStype_FRAGMENT_TEXT_START:
      case WStype_FRAGMENT_BIN_START:
      case WStype_FRAGMENT:
      case WStype_FRAGMENT_FIN: {
        telemetry_stats_touch(clientId, nowMs);
        break;
      }
    }
  }

#endif // ARDUINO

#ifdef ARDUINO
  // --- Параметры батареи для оценки процента заряда ---
  constexpr float BATTERY_MIN_V = 6.4f;  ///< Нижний порог напряжения (полностью разряжено, 2S Li-Ion).
  constexpr float BATTERY_MAX_V = 8.4f;  ///< Верхний порог напряжения (полный заряд).

  WebServer gServer(80);                 ///< Простой HTTP-сервер.
  unsigned long gLastWifiLog = 0;        ///< Время последнего вывода статуса Wi-Fi.

  /**
   * \brief Безопасное ограничение значения в заданных пределах.
   */
  float clampf(float value, float minV, float maxV) {
    if (value < minV) return minV;
    if (value > maxV) return maxV;
    return value;
  }

  /**
   * \brief Пересчёт напряжения батареи в проценты заряда.
   */
  float estimate_battery_percent(float voltage) {
    const float normalized = (voltage - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V);
    const float clamped = clampf(normalized, 0.0f, 1.0f);
    return clamped * 100.0f;
  }

  /**
   * \brief Формирует текстовое описание команды для логов.
   */
  String describe_command(const Command& cmd) {
    String description;
    switch (cmd.action) {
      case Action::Move:
        description = F("Move ");
        description += (cmd.direction == Direction::Forward) ? F("Forward") : F("Backward");
        description += F(" distance=");
        description += String(cmd.value, 3);
        description += F("m");
        break;
      case Action::Rotate:
        description = F("Rotate ");
        description += (cmd.direction == Direction::Forward) ? F("Left") : F("Right");
        description += F(" angle=");
        description += String(cmd.value, 1);
        description += F("deg");
        break;
      case Action::EmergencyStop:
        description = F("EmergencyStop");
        break;
    }
    if (cmd.duty > 0) {
      description += F(" duty=");
      description += cmd.duty;
    }
    return description;
  }

  /**
   * \brief Экранирует спецсимволы для JSON-строк.
   */
  String json_escape(const std::string& text) {
    String out;
    out.reserve(text.size() + 4);
    for (unsigned char c : text) {
      switch (c) {
        case '\\': out += F("\\\\"); break;
        case '"':  out += F("\\\""); break;
        case '\b': out += F("\\b"); break;
        case '\f': out += F("\\f"); break;
        case '\n': out += F("\\n"); break;
        case '\r': out += F("\\r"); break;
        case '\t': out += F("\\t"); break;
        default:
          if (c < 0x20) {
            char buf[7];
            // Используем двойное экранирование, чтобы в JSON получилось буквальное "\uXXXX".
            // Если оставить один обратный слэш, препроцессор Arduino сочтёт последовательность \u незавершённой
            // и выдаст ошибку компиляции при сборке прошивки.
            snprintf(buf, sizeof(buf), "\\\\u%04X", static_cast<unsigned>(c));
            out += buf;
          } else {
            out += static_cast<char>(c);
          }
          break;
      }
    }
    return out;
  }

  /**
   * \brief Возвращает HTML-страницу с интерфейсом управления.
   *        Компактная сетка упорядочивает управление и конфигурацию в две колонки,
   *        а статусная панель располагается сверху для быстрой оценки состояния.
   */
  String render_root_page() {
    // Используем поэтапное построение строки, чтобы избежать ошибок препроцессора Arduino
    // при работе с макросом F и длинными строковыми литералами.
    String html;
    html.reserve(8192);

    // --- Заголовок документа и базовые стили интерфейса ---
    html += F(R"rawl(<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><title>Home Robot Remote</title><meta name="viewport" content="width=device-width, initial-scale=1"><style>body{font-family:Arial,Helvetica,sans-serif;background:#101820;color:#f0f0f0;margin:0;}header{background:#0072ce;padding:14px;text-align:center;font-size:1.2rem;font-weight:600;letter-spacing:.02em;}main{max-width:1200px;margin:0 auto;padding:16px 16px 40px;}.status-wrapper{background:#132033;border-radius:10px;padding:12px 16px;margin-bottom:20px;box-shadow:0 0 12px rgba(0,0,0,0.35);}.status-title{font-size:0.95rem;font-weight:600;color:#9ec9ff;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;}/* Переносим длинные значения телеметрии на несколько строк */.status-panel{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;align-items:stretch;}.status-card{background:#1b2738;border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;box-shadow:0 0 8px rgba(0,0,0,0.3);}.status-card .label{font-size:0.75rem;color:#b9c6d2;text-transform:uppercase;letter-spacing:.05em;}.status-card .value{font-size:1.05rem;font-weight:600;white-space:pre-wrap;line-height:1.3;word-break:break-word;overflow-wrap:anywhere;}.content-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;align-items:flex-start;}.panel{background:#132033;border-radius:10px;padding:16px;box-shadow:0 0 12px rgba(0,0,0,0.35);}.panel h2{margin:0 0 12px;color:#9ec9ff;font-size:1.05rem;letter-spacing:.02em;}.control-grid{display:grid;gap:12px;}.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px 16px;}.form-field{display:flex;flex-direction:column;gap:4px;font-size:0.9rem;}.form-field span{color:#c6d6e4;}.form-field input{padding:8px;border-radius:6px;border:1px solid #2a9df4;background:#0f1724;color:#f0f6ff;font-size:0.95rem;}.form-field input:focus{outline:none;border-color:#4fb3ff;box-shadow:0 0 0 2px rgba(79,179,255,0.2);}.dual-buttons{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;}button{padding:10px 12px;border:none;border-radius:6px;font-size:0.95rem;font-weight:600;color:#fff;background:#2a9df4;cursor:pointer;transition:transform .1s ease,background .2s ease;}button:hover{background:#2386c5;}button:active{transform:scale(0.98);}button:disabled{background:#3a4b61;cursor:not-allowed;}.danger{background:#d7263d;}.danger:hover{background:#b51f33;}.primary{grid-column:1/-1;margin-top:4px;}.checkbox{flex-direction:row;align-items:center;gap:10px;}.checkbox input{width:auto;height:18px;}.checkbox span{color:#c6d6e4;font-size:0.9rem;}#log{background:#0b111a;min-height:120px;max-height:260px;overflow-y:auto;border-radius:8px;padding:12px;font-family:monospace;font-size:0.9rem;line-height:1.35;box-shadow:inset 0 0 0 1px rgba(42,157,244,0.25);}.log-panel h2{margin-bottom:8px;}@media(max-width:900px){.status-panel{grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;}}@media(max-width:600px){header{font-size:1.05rem;padding:12px;}button{font-size:0.9rem;}.status-card .value{font-size:1rem;}}</style></head><body><header>Удалённое управление домашним роботом</header><main><section class="status-wrapper"><div class="status-title">Текущий статус</div><div class="status-panel">)rawl");
    html += F(R"rawl(<div class="status-card"><div class="label">Напряжение</div><div class="value" id="busVoltage">0.00 В</div></div><div class="status-card"><div class="label">Ток</div><div class="value" id="current">0.00 А</div></div><div class="status-card"><div class="label">Мощность</div><div class="value" id="power">0.00 Вт</div></div><div class="status-card"><div class="label">Уровень АКБ</div><div class="value" id="battery">0 %</div></div><div class="status-card"><div class="label">Курс</div><div class="value" id="heading">0 °</div></div><div class="status-card"><div class="label">Скорость поворота</div><div class="value" id="turnRate">0 °/с</div></div><div class="status-card"><div class="label">Смещение гироскопа</div><div class="value" id="gyroBias">0 °/с</div></div><div class="status-card"><div class="label">Тики (лев/прав)</div><div class="value" id="ticks">0 / 0</div></div><div class="status-card"><div class="label">Скорости (м/с)</div><div class="value" id="velocities">0 / 0</div></div><div class="status-card"><div class="label">Дистанции (м)</div><div class="value" id="distances">0 / 0</div></div><div class="status-card"><div class="label">Температура</div><div class="value" id="temperature">0 °C</div></div><div class="status-card"><div class="label">Локализация звука</div><div class="value" id="audioDirection">сервер</div></div><div class="status-card"><div class="label">Громкость L/R</div><div class="value" id="audioLevels">0 / 0</div></div><div class="status-card"><div class="label">Поток аудио</div><div class="value" id="audioStream">ожидание</div></div><div class="status-card"><div class="label">Воспроизведение</div><div class="value" id="audioPlayback">ожидание</div></div><div class="status-card"><div class="label">Параметры аудио</div><div class="value" id="audioMeta">0 Гц</div></div><div class="status-card"><div class="label">WS телеметрии</div><div class="value" id="telemetryWs">0 / 0</div></div></div></div></div></section><div class="content-grid"><section class="panel"><h2>Управление движением</h2><div class="control-grid"><label class="form-field"><span>Расстояние (метры)</span><input id="moveDistance" type="number" step="0.05" min="0" value="0.50"></label><label class="form-field"><span>Duty (0 = по умолчанию)</span><input id="moveDuty" type="number" min="0" max="1023" value="0"></label><div class="dual-buttons"><button data-lock-on-busy="1" onclick="sendMove('forward')">Вперёд</button><button data-lock-on-busy="1" onclick="sendMove('backward')">Назад</button></div><label class="form-field"><span>Угол (градусы)</span><input id="rotateAngle" type="number" step="1" min="0" value="90"></label><label class="form-field"><span>Duty (0 = по умолчанию)</span><input id="rotateDuty" type="number" min="0" max="1023" value="0"></label><div class="dual-buttons"><button data-lock-on-busy="1" onclick="sendRotate('left')">Влево</button><button data-lock-on-busy="1" onclick="sendRotate('right')">Вправо</button></div><button class="danger" onclick="sendStop()">Аварийная остановка</button></div></section>)rawl");
    html += F(R"rawl(<section class="panel"><h2>Параметры движения</h2><div class="form-grid"><label class="form-field"><span>Диаметр колеса (мм)</span><input id="wheelDiameter" type="number" step="0.1" min="30" max="200"></label><label class="form-field"><span>База (мм)</span><input id="wheelBase" type="number" step="0.1" min="100" max="500"></label><label class="form-field"><span>TPR левый</span><input id="tprLeft" type="number" step="1" min="100" max="20000"></label><label class="form-field"><span>TPR правый</span><input id="tprRight" type="number" step="1" min="100" max="20000"></label><label class="form-field"><span>KP прямой ход (энкодеры)</span><input id="kpStraightEnc" type="number" step="0.01" min="0" max="20"></label><label class="form-field"><span>KP прямой ход (гиро)</span><input id="kpStraightGyro" type="number" step="0.01" min="0" max="40"></label><label class="form-field"><span>KI прямой ход (гиро)</span><input id="kiStraightGyro" type="number" step="0.01" min="0" max="20"></label><label class="form-field"><span>KP поворот (энкодеры)</span><input id="kpTurnEnc" type="number" step="0.01" min="0" max="20"></label><label class="form-field"><span>KP поворот (гиро)</span><input id="kpTurnGyro" type="number" step="0.01" min="0" max="40"></label><label class="form-field"><span>KI поворот (гиро)</span><input id="kiTurnGyro" type="number" step="0.01" min="0" max="20"></label><label class="form-field"><span>Допуск угла (°)</span><input id="headingTolerance" type="number" step="0.1" min="0" max="10"></label><label class="form-field"><span>Порог застревания (°/с)</span><input id="stuckRate" type="number" step="0.1" min="0" max="50"></label><label class="form-field"><span>Таймаут застревания (мс)</span><input id="stuckTimeout" type="number" step="10" min="100" max="5000"></label><label class="form-field"><span>Лимит интеграла (°·с)</span><input id="yawIntegralLimit" type="number" step="0.1" min="0" max="200"></label><label class="form-field checkbox"><input id="enableGyro" type="checkbox"><span>Использовать гироскоп</span></label><label class="form-field checkbox"><input id="useGyroStraight" type="checkbox"><span>Гироскоп для прямого/обратного хода</span></label><button id="saveParamsBtn" class="primary" onclick="saveParams()">Сохранить параметры</button></div></section></div><section class="panel log-panel"><h2>Лог</h2><div id="log"></div></section>)rawl");

    // --- JavaScript блок с логикой ---
    html += F(R"rawl(</main><script>
const BUSY_LOCK_SELECTOR='[data-lock-on-busy="1"]';/* Селектор кнопок, блокируемых при выполнении движения */
const TELEMETRY_WS_PORT=81;
const TELEMETRY_WS_PATH='/';
const TELEMETRY_RECONNECT_MS=2000;
const STATUS_FALLBACK_PERIOD_MS=5000;
let statusSocket=null;
let statusReconnectTimer=null;
let fallbackTimer=null;

function applyBusyState(busy){
  document.querySelectorAll(BUSY_LOCK_SELECTOR).forEach(btn=>{btn.disabled=busy;});
  const saveBtn=document.getElementById('saveParamsBtn');
  if(saveBtn){saveBtn.disabled=busy;}
}

function appendLog(text){
  const log=document.getElementById('log');
  const ts=new Date().toLocaleTimeString();
  const entry=document.createElement('div');
  entry.textContent=`[${ts}] ${text}`;
  log.prepend(entry);
  while(log.childNodes.length>50){log.removeChild(log.lastChild);}
}

function applyStatusPayload(data){
  try{
    document.getElementById('busVoltage').textContent=`${data.busVoltage.toFixed(2)} В`;
    document.getElementById('current').textContent=`${data.currentA.toFixed(2)} А`;
    document.getElementById('power').textContent=`${data.powerW.toFixed(2)} Вт`;
    document.getElementById('battery').textContent=`${data.batteryPercent.toFixed(0)} %`;
    document.getElementById('heading').textContent=`${data.headingDeg.toFixed(1)} °`;
    document.getElementById('turnRate').textContent=`${data.turnRateDps.toFixed(1)} °/с`;
    document.getElementById('gyroBias').textContent=`${data.gyroBiasDps.toFixed(2)} °/с`;
    document.getElementById('ticks').textContent=`${data.ticksLeft} / ${data.ticksRight}`;
    document.getElementById('velocities').textContent=`${data.velocityLeft.toFixed(3)} / ${data.velocityRight.toFixed(3)}`;
    document.getElementById('distances').textContent=`${data.distanceLeft.toFixed(3)} / ${data.distanceRight.toFixed(3)}`;
    document.getElementById('temperature').textContent=`${data.temperatureC.toFixed(1)} °C`;
    let audioDirectionText='сервер';
    if(data.audioLocalizationActive){
      const directionValue=isFinite(data.audioDirectionDeg)?`${data.audioDirectionDeg.toFixed(1)}°`:'—';
      const confidenceValue=Math.round(Math.max(0,Math.min(1,data.audioConfidence))*100);
      audioDirectionText=`${directionValue} (${confidenceValue}%)`;
    }
    document.getElementById('audioDirection').textContent=audioDirectionText;
    document.getElementById('audioLevels').textContent=`${data.audioRmsLeft.toFixed(3)} / ${data.audioRmsRight.toFixed(3)}`;
    const streamParts=[];
    if(!data.audioStreamEndpoint){
      streamParts.push('отключено');
    }else{
      streamParts.push(data.audioStreamReady?'готов':'ожидание');
      if(data.audioStreamSent!==undefined&&data.audioStreamFailed!==undefined){
        streamParts.push(`ok:${data.audioStreamSent} err:${data.audioStreamFailed}`);
      }
      if(typeof data.audioStreamQueueDepth!=='undefined'){
        const queueInfo=data.audioStreamQueueHigh!==undefined?`${data.audioStreamQueueDepth}/${data.audioStreamQueueHigh}`:`${data.audioStreamQueueDepth}`;
        streamParts.push(`queue:${queueInfo}`);
      }
      if(typeof data.audioStreamQueueDrops!=='undefined'&&data.audioStreamQueueDrops>0){
        streamParts.push(`drop:${data.audioStreamQueueDrops}`);
      }
      if(typeof data.audioStreamWsOfflineDrops!=='undefined'&&data.audioStreamWsOfflineDrops>0){
        streamParts.push(`offline:${data.audioStreamWsOfflineDrops}`);
      }
      if(typeof data.audioStreamQueueStalls!=='undefined'&&data.audioStreamQueueStalls>0){
        streamParts.push(`stall:${data.audioStreamQueueStalls}`);
      }
      if(typeof data.audioStreamWsConnected!=='undefined'){
        streamParts.push(data.audioStreamWsConnected?'ws:on':'ws:off');
      }
      if(typeof data.audioStreamWsReconnects!=='undefined'){
        streamParts.push(`recon:${data.audioStreamWsReconnects}`);
      }
      if(typeof data.audioStreamWsTimeouts!=='undefined'&&data.audioStreamWsTimeouts>0){
        streamParts.push(`timeout:${data.audioStreamWsTimeouts}`);
      }
      if(typeof data.audioStreamBytes!=='undefined'){
        streamParts.push(`bytes:${data.audioStreamBytes}`);
      }
      if(typeof data.audioStreamLastDurationMs!=='undefined'){
        streamParts.push(`${Math.round(data.audioStreamLastDurationMs)}мс`);
      }
      if(data.audioStreamLastError){
        streamParts.push(data.audioStreamLastError);
      }
      streamParts.push(data.audioStreamEndpoint);
    }
    // Отрисовываем карточку аудиопотока построчно, отдавая приоритет массиву, сформированному на стороне прошивки.
    const summaryParts=Array.isArray(data.audioStreamSummary)&&data.audioStreamSummary.length>0?data.audioStreamSummary:streamParts;
    document.getElementById('audioStream').textContent=summaryParts.join('\n');
    const playbackEl=document.getElementById('audioPlayback');
    if(playbackEl){
      const playbackParts=[];
      playbackParts.push(data.audioPlaybackReady?'готово':'нет сигнала');
      if(typeof data.audioPlaybackSampleRate==='number'&&data.audioPlaybackSampleRate>0){
        playbackParts.push(`${data.audioPlaybackSampleRate} Гц`);
      }
      if(typeof data.audioPlaybackVolume==='number'&&data.audioPlaybackVolume>0){
        playbackParts.push(`vol:${data.audioPlaybackVolume.toFixed(2)}`);
      }
      if(typeof data.audioPlaybackQueueDepth==='number'){
        if(typeof data.audioPlaybackQueueHigh==='number'&&data.audioPlaybackQueueHigh>0){
          playbackParts.push(`queue:${data.audioPlaybackQueueDepth}/${data.audioPlaybackQueueHigh}`);
        }else{
          playbackParts.push(`queue:${data.audioPlaybackQueueDepth}`);
        }
      }
      if(typeof data.audioPlaybackChunksAccepted==='number'){
        playbackParts.push(`in:${data.audioPlaybackChunksAccepted}`);
      }
      if(typeof data.audioPlaybackChunksPlayed==='number'){
        playbackParts.push(`played:${data.audioPlaybackChunksPlayed}`);
      }
      if(typeof data.audioPlaybackChunksRejected==='number'&&data.audioPlaybackChunksRejected>0){
        playbackParts.push(`rej:${data.audioPlaybackChunksRejected}`);
      }
      if(typeof data.audioPlaybackDrops==='number'&&data.audioPlaybackDrops>0){
        playbackParts.push(`drop:${data.audioPlaybackDrops}`);
      }
      if(typeof data.audioPlaybackUnderruns==='number'&&data.audioPlaybackUnderruns>0){
        playbackParts.push(`und:${data.audioPlaybackUnderruns}`);
      }
      if(typeof data.audioPlaybackLastSeq==='number'&&data.audioPlaybackLastSeq>0){
        playbackParts.push(`#${data.audioPlaybackLastSeq}`);
      }
      if(data.audioPlaybackLastError){
        playbackParts.push(data.audioPlaybackLastError);
      }
      playbackEl.textContent=playbackParts.join('\n');
    }
    const metaParts=[];
    if(data.audioSampleRate){metaParts.push(`${data.audioSampleRate} Гц`);}
    if(data.audioFrameSamples){metaParts.push(`${data.audioFrameSamples} сэмп.`);}
    if(typeof data.audioMicSpacingMeters==='number'&&data.audioMicSpacingMeters>0){metaParts.push(`${data.audioMicSpacingMeters.toFixed(3)} м`);}
    metaParts.push(data.audioLocalizationActive?'угол на борту':'угол на сервере');
    if(typeof data.telemetryClients==='number'){metaParts.push(`ws:${data.telemetryClients}`);}
    document.getElementById('audioMeta').textContent=metaParts.join(' · ');
    const telemEl=document.getElementById('telemetryWs');
    if(telemEl){
      const clients=typeof data.telemetryClients==='number'?data.telemetryClients:0;
      const maxClients=typeof data.telemetryClientsMax==='number'?data.telemetryClientsMax:0;
      const messages=typeof data.telemetryMessages==='number'?data.telemetryMessages:0;
      const duplicates=typeof data.telemetryDuplicates==='number'?data.telemetryDuplicates:0;
      let latencyPart='';
      if(typeof data.statusTimestampMs==='number'&&typeof data.telemetryLastBroadcastMs==='number'&&data.telemetryLastBroadcastMs>0){
        const deltaMs=Math.max(0,data.statusTimestampMs-data.telemetryLastBroadcastMs);
        latencyPart=` · Δ${(deltaMs/1000).toFixed(1)}с`;
      }
      let errorPart='';
      if(data.telemetryLastError){
        errorPart=` · err:${data.telemetryLastError}`;
      }
      telemEl.textContent=`${clients}/${maxClients} · msg:${messages} · dup:${duplicates}${latencyPart}${errorPart}`;
    }
    applyBusyState(data.busy);
  }catch(err){
    appendLog('Ошибка обработки статуса: '+err.message);
  }
}

async function sendMove(direction){
  const distance=parseFloat(document.getElementById('moveDistance').value)||0;
  const duty=parseInt(document.getElementById('moveDuty').value)||0;
  await sendCommand(`/api/move?direction=${direction}&distance=${distance}&duty=${duty}`);
}

async function sendRotate(direction){
  const angle=parseFloat(document.getElementById('rotateAngle').value)||0;
  const duty=parseInt(document.getElementById('rotateDuty').value)||0;
  await sendCommand(`/api/rotate?direction=${direction}&angle=${angle}&duty=${duty}`);
}

async function sendStop(){
  await sendCommand('/api/stop');
}

async function sendCommand(url){
  applyBusyState(true);
  try{
    const res=await fetch(url);
    const data=await res.json();
    appendLog(data.message||'Команда отправлена');
  }catch(err){
    appendLog('Ошибка: '+err.message);
  }
  await refreshStatus(true);
}

async function refreshStatus(force=false){
  if(!force && statusSocket && statusSocket.readyState===WebSocket.OPEN){
    return;
  }
  try{
    const res=await fetch('/api/status');
    const data=await res.json();
    applyStatusPayload(data);
  }catch(err){
    appendLog('Не удалось обновить статус: '+err.message);
    applyBusyState(false);
  }
}

function setNumber(id,value,digits){
  const el=document.getElementById(id);
  if(!el)return;
  el.value=(typeof digits==='number')?Number(value).toFixed(digits):value;
}

function setInteger(id,value){
  const el=document.getElementById(id);
  if(!el)return;
  el.value=Math.round(value);
}

async function loadParams(){
  try{
    const res=await fetch('/api/params');
    const data=await res.json();
    setNumber('wheelDiameter',data.wheelDiameterMm,2);
    setNumber('wheelBase',data.wheelBaseMm,2);
    setInteger('tprLeft',data.tprLeft);
    setInteger('tprRight',data.tprRight);
    setNumber('kpStraightEnc',data.kpStraightEnc,2);
    setNumber('kpStraightGyro',data.kpStraightGyro,2);
    setNumber('kiStraightGyro',data.kiStraightGyro,2);
    setNumber('kpTurnEnc',data.kpTurnEnc,2);
    setNumber('kpTurnGyro',data.kpTurnGyro,2);
    setNumber('kiTurnGyro',data.kiTurnGyro,2);
    setNumber('headingTolerance',data.headingToleranceDeg,2);
    setNumber('stuckRate',data.stuckRateThresholdDps,2);
    setInteger('stuckTimeout',data.stuckTimeoutMs);
    setNumber('yawIntegralLimit',data.yawIntegralLimit,2);
    document.getElementById('enableGyro').checked=!!data.enableGyro;
    document.getElementById('useGyroStraight').checked=!!data.useGyroStraight;
  }catch(err){
    appendLog('Не удалось загрузить параметры: '+err.message);
  }
}

async function saveParams(){
  const saveBtn=document.getElementById('saveParamsBtn');
  const originalLabel=saveBtn?saveBtn.textContent:'';
  if(saveBtn){saveBtn.textContent='Сохраняем...';}
  const params=new URLSearchParams();
  params.append('wheelDiameterMm',document.getElementById('wheelDiameter').value);
  params.append('wheelBaseMm',document.getElementById('wheelBase').value);
  params.append('tprLeft',document.getElementById('tprLeft').value);
  params.append('tprRight',document.getElementById('tprRight').value);
  params.append('kpStraightEnc',document.getElementById('kpStraightEnc').value);
  params.append('kpStraightGyro',document.getElementById('kpStraightGyro').value);
  params.append('kiStraightGyro',document.getElementById('kiStraightGyro').value);
  params.append('kpTurnEnc',document.getElementById('kpTurnEnc').value);
  params.append('kpTurnGyro',document.getElementById('kpTurnGyro').value);
  params.append('kiTurnGyro',document.getElementById('kiTurnGyro').value);
  params.append('headingToleranceDeg',document.getElementById('headingTolerance').value);
  params.append('stuckRateThresholdDps',document.getElementById('stuckRate').value);
  params.append('stuckTimeoutMs',document.getElementById('stuckTimeout').value);
  params.append('yawIntegralLimit',document.getElementById('yawIntegralLimit').value);
  params.append('enableGyro',document.getElementById('enableGyro').checked?'1':'0');
  params.append('useGyroStraight',document.getElementById('useGyroStraight').checked?'1':'0');
  try{
    const res=await fetch('/api/params',{method:'POST',body:params});
    const data=await res.json();
    if(data.success){
      appendLog('Параметры сохранены');
    }else{
      appendLog('Ошибка сохранения параметров: '+(data.message||'неизвестно'));
    }
    await loadParams();
    await refreshStatus(true);
  }catch(err){
    appendLog('Ошибка сохранения параметров: '+err.message);
    applyBusyState(false);
  }finally{
    if(saveBtn){saveBtn.textContent=originalLabel||'Сохранить параметры';}
  }
}

function buildTelemetryUrl(){
  const protocol=location.protocol==='https:'?'wss':'ws';
  const host=location.hostname||location.host;
  const portSegment=TELEMETRY_WS_PORT?`:${TELEMETRY_WS_PORT}`:'';
  return `${protocol}://${host}${portSegment}${TELEMETRY_WS_PATH}`;
}

function scheduleStatusReconnect(reason){
  if(statusReconnectTimer){clearTimeout(statusReconnectTimer);}
  appendLog('WS телеметрии: '+reason);
  statusReconnectTimer=setTimeout(()=>{connectStatusSocket();},TELEMETRY_RECONNECT_MS);
}

function connectStatusSocket(){
  if(statusSocket){
    try{statusSocket.close();}catch(_){ }
    statusSocket=null;
  }
  const url=buildTelemetryUrl();
  appendLog('WS телеметрии подключение: '+url);
  try{
    statusSocket=new WebSocket(url);
  }catch(err){
    appendLog('WS телеметрии не удалось создать: '+err.message);
    scheduleStatusReconnect('ошибка конструктора');
    return;
  }
  statusSocket.addEventListener('open',()=>{
    appendLog('WS телеметрии открыт');
    if(statusReconnectTimer){clearTimeout(statusReconnectTimer);statusReconnectTimer=null;}
    try{statusSocket.send('hello-ui');}catch(_){ }
  });
  statusSocket.addEventListener('message',event=>{
    try{
      const payload=JSON.parse(event.data);
      applyStatusPayload(payload);
    }catch(err){
      appendLog('WS телеметрии JSON ошибка: '+err.message);
    }
  });
  statusSocket.addEventListener('close',()=>{
    scheduleStatusReconnect('соединение закрыто');
  });
  statusSocket.addEventListener('error',()=>{
    appendLog('WS телеметрии: ошибка соединения');
    if(statusSocket){try{statusSocket.close();}catch(_){ }}
  });
}

function ensureFallbackTimer(){
  if(!fallbackTimer){
    fallbackTimer=setInterval(()=>{refreshStatus(false);},STATUS_FALLBACK_PERIOD_MS);
  }
}

window.addEventListener('beforeunload',()=>{
  if(statusSocket){
    try{statusSocket.close();}catch(_){ }
  }
});

ensureFallbackTimer();
connectStatusSocket();
refreshStatus(true);
loadParams();
</script></body></html>)rawl");

    return html;
  }
  String render_status_json() {
    const auto streamCfg = snapshot_audio_stream_config();
    const auto streamStats = snapshot_audio_stream_stats();
    const auto telemetryStats = snapshot_telemetry_stats();
    const auto playbackStats = AudioPlayback::stats();

    const unsigned long nowMs = millis();
    String json = F("{");
    json += F("\"busVoltage\":"); json += String(gDiagnostics.busVoltage, 3);
    json += F(",\"currentA\":"); json += String(gDiagnostics.currentA, 3);
    json += F(",\"powerW\":"); json += String(gDiagnostics.powerW, 3);
    json += F(",\"batteryPercent\":"); json += String(gDiagnostics.batteryPercent, 1);
    json += F(",\"headingDeg\":"); json += String(gDiagnostics.headingDeg, 2);
    json += F(",\"turnRateDps\":"); json += String(gDiagnostics.turnRateDps, 2);
    json += F(",\"gyroBiasDps\":"); json += String(gDiagnostics.gyroBiasDps, 2);
    json += F(",\"ticksLeft\":"); json += String(gDiagnostics.ticksLeft);
    json += F(",\"ticksRight\":"); json += String(gDiagnostics.ticksRight);
    json += F(",\"velocityLeft\":"); json += String(gDiagnostics.velocityLeft, 3);
    json += F(",\"velocityRight\":"); json += String(gDiagnostics.velocityRight, 3);
    json += F(",\"distanceLeft\":"); json += String(gDiagnostics.distanceLeft, 3);
    json += F(",\"distanceRight\":"); json += String(gDiagnostics.distanceRight, 3);
    json += F(",\"temperatureC\":"); json += String(gDiagnostics.temperatureC, 2);
    json += F(",\"audioLocalizationActive\":"); json += gDiagnostics.audioLocalizationActive ? F("true") : F("false");
    json += F(",\"audioDirectionDeg\":"); json += String(gDiagnostics.audioDirectionDeg, 2);
    json += F(",\"audioConfidence\":"); json += String(gDiagnostics.audioConfidence, 3);
    json += F(",\"audioRmsLeft\":"); json += String(gDiagnostics.audioRmsLeft, 3);
    json += F(",\"audioRmsRight\":"); json += String(gDiagnostics.audioRmsRight, 3);
    json += F(",\"audioSampleRate\":"); json += String(gDiagnostics.audioSampleRate);
    json += F(",\"audioFrameSamples\":"); json += String(gDiagnostics.audioFrameSamples);
    json += F(",\"audioStreamReady\":"); json += gDiagnostics.audioStreamReady ? F("true") : F("false");
    json += F(",\"audioMicSpacingMeters\":"); json += String(gDiagnostics.audioMicSpacingMeters, 3);
    json += F(",\"statusTimestampMs\":"); json += String(nowMs);
    json += F(",\"audioStreamEndpoint\":\"");
    json += json_escape(streamCfg.endpoint);
    json += '\"';
    json += F(",\"audioStreamSent\":"); json += String(static_cast<unsigned long>(streamStats.framesSent));
    json += F(",\"audioStreamFailed\":"); json += String(static_cast<unsigned long>(streamStats.framesFailed));
    json += F(",\"audioStreamNext\":"); json += String(static_cast<unsigned long>(streamStats.nextSequence));
    json += F(",\"audioStreamLastDurationMs\":"); json += String(static_cast<unsigned long>(streamStats.lastDurationMs));
    json += F(",\"audioStreamLastAttemptMs\":"); json += String(static_cast<unsigned long>(streamStats.lastAttemptMs));
    json += F(",\"audioStreamLastOk\":"); json += streamStats.lastAttemptOk ? F("true") : F("false");
    json += F(",\"audioStreamBytes\":"); json += String(static_cast<unsigned long>(streamStats.bytesSent));
    json += F(",\"audioStreamQueueDepth\":"); json += String(static_cast<unsigned long>(streamStats.queueDepth));
    json += F(",\"audioStreamQueueHigh\":"); json += String(static_cast<unsigned long>(streamStats.queueHighWatermark));
    json += F(",\"audioStreamQueueDrops\":"); json += String(static_cast<unsigned long>(streamStats.queueDrops));
    json += F(",\"audioStreamQueueStalls\":"); json += String(static_cast<unsigned long>(streamStats.queueStalls));
    json += F(",\"audioStreamWsOfflineDrops\":"); json += String(static_cast<unsigned long>(streamStats.wsOfflineDrops));
    json += F(",\"audioStreamWsConnected\":"); json += streamStats.wsConnected ? F("true") : F("false");
    json += F(",\"audioStreamWsReconnects\":"); json += String(static_cast<unsigned long>(streamStats.wsReconnects));
    json += F(",\"audioStreamWsTimeouts\":"); json += String(static_cast<unsigned long>(streamStats.wsTimeouts));
    json += F(",\"audioStreamWsLastConnectMs\":"); json += String(static_cast<unsigned long>(streamStats.wsLastConnectMs));
    json += F(",\"audioStreamWsLastDisconnectMs\":"); json += String(static_cast<unsigned long>(streamStats.wsLastDisconnectMs));
    json += F(",\"audioStreamLastError\":\"");
    json += json_escape(streamStats.lastError);
    json += '\"';

    // Добавляем предсобранный массив коротких меток, чтобы UI мог выводить их построчно без повторного форматирования.
    const auto audioSummary = detail::build_audio_stream_summary(gAudioStreamConfig, streamStats, gDiagnostics);
    json += F(",\"audioStreamSummary\":[");
    for (size_t i = 0; i < audioSummary.size(); ++i) {
      if (i > 0) {
        json += ',';
      }
      json += '\"';
      json += json_escape(audioSummary[i]);
      json += '\"';
    }
    json += ']';
    const bool playbackReady = playbackStats.initialized &&
                               (playbackStats.chunksPlayed > 0 || playbackStats.chunksAccepted > 0);
    json += F(",\"audioPlaybackReady\":"); json += playbackReady ? F("true") : F("false");
    json += F(",\"audioPlaybackChunksAccepted\":"); json += String(static_cast<unsigned long>(playbackStats.chunksAccepted));
    json += F(",\"audioPlaybackChunksRejected\":"); json += String(static_cast<unsigned long>(playbackStats.chunksRejected));
    json += F(",\"audioPlaybackChunksPlayed\":"); json += String(static_cast<unsigned long>(playbackStats.chunksPlayed));
    json += F(",\"audioPlaybackQueueDepth\":"); json += String(static_cast<unsigned long>(playbackStats.queueDepth));
    json += F(",\"audioPlaybackQueueHigh\":"); json += String(static_cast<unsigned long>(playbackStats.queueHighWatermark));
    json += F(",\"audioPlaybackDrops\":"); json += String(static_cast<unsigned long>(playbackStats.queueDrops));
    json += F(",\"audioPlaybackUnderruns\":"); json += String(static_cast<unsigned long>(playbackStats.bufferUnderruns));
    json += F(",\"audioPlaybackLastSeq\":"); json += String(static_cast<unsigned long>(playbackStats.lastSequence));
    json += F(",\"audioPlaybackSampleRate\":"); json += String(static_cast<unsigned long>(playbackStats.lastSampleRate));
    json += F(",\"audioPlaybackVolume\":"); json += String(playbackStats.lastVolume, 2);
    json += F(",\"audioPlaybackLastError\":\"");
    json += json_escape(playbackStats.lastError);
    json += '\"';
    json += F(",\"telemetryClients\":"); json += String(static_cast<unsigned long>(telemetryStats.clientsConnected));
    json += F(",\"telemetryClientsMax\":"); json += String(static_cast<unsigned long>(telemetryStats.clientsMax));
    json += F(",\"telemetryMessages\":"); json += String(static_cast<unsigned long>(telemetryStats.messagesSent));
    json += F(",\"telemetryBytes\":"); json += String(static_cast<unsigned long>(telemetryStats.bytesSent));
    json += F(",\"telemetryLastPayload\":"); json += String(static_cast<unsigned long>(telemetryStats.lastPayloadBytes));
    json += F(",\"telemetryDuplicates\":"); json += String(static_cast<unsigned long>(telemetryStats.duplicatesSkipped));
    json += F(",\"telemetryLastBroadcastMs\":");
    json += String(static_cast<unsigned long>(telemetryStats.lastBroadcastMs));
    json += F(",\"telemetryLastEventMs\":"); json += String(static_cast<unsigned long>(telemetryStats.lastEventMs));
    json += F(",\"telemetryLastClient\":"); json += String(static_cast<unsigned long>(telemetryStats.lastClientId));
    json += F(",\"telemetryLastError\":\"");
    json += json_escape(telemetryStats.lastError);
    json += '\"';
    json += F(",\"busy\":"); json += gCommandInProgress ? F("true") : F("false");
    json += F(",\"telemetryFrozen\":false"); // телеметрия всегда доступна даже во время движения
    json += F("}");
    return json;
  }

  /**
   * \brief Формирует JSON со всеми настраиваемыми параметрами движения.
   */
  String render_config_json() {
    const auto& cfg = ConfigStore::current_config();
    const auto env  = ConfigStore::environment();

    String json = F("{");
    json += F("\"wheelDiameterMm\":"); json += String(cfg.wheel_diameter_mm, 3);
    json += F(",\"wheelBaseMm\":"); json += String(cfg.wheel_base_mm, 3);
    json += F(",\"tprLeft\":"); json += String(cfg.tpr_left);
    json += F(",\"tprRight\":"); json += String(cfg.tpr_right);
    json += F(",\"kpStraightEnc\":"); json += String(cfg.kp_straight_enc, 3);
    json += F(",\"kpStraightGyro\":"); json += String(cfg.kp_straight_gyro, 3);
    json += F(",\"kiStraightGyro\":"); json += String(cfg.ki_straight_gyro, 3);
    json += F(",\"kpTurnEnc\":"); json += String(cfg.kp_turn_enc, 3);
    json += F(",\"kpTurnGyro\":"); json += String(cfg.kp_turn_gyro, 3);
    json += F(",\"kiTurnGyro\":"); json += String(cfg.ki_turn_gyro, 3);
    json += F(",\"headingToleranceDeg\":"); json += String(cfg.heading_tolerance_deg, 3);
    json += F(",\"stuckRateThresholdDps\":"); json += String(cfg.stuck_rate_threshold_dps, 3);
    json += F(",\"stuckTimeoutMs\":"); json += String(static_cast<unsigned long>(cfg.stuck_timeout_ms));
    json += F(",\"yawIntegralLimit\":"); json += String(cfg.yaw_integral_limit, 3);
    json += F(",\"enableGyro\":"); json += cfg.enable_gyro_feedback ? F("true") : F("false");
    json += F(",\"useGyroStraight\":"); json += cfg.use_gyro_for_straight ? F("true") : F("false");
    json += F(",\"gyroAvailable\":"); json += env.gyro_available ? F("true") : F("false");
    json += F(",\"busy\":"); json += gCommandInProgress ? F("true") : F("false");
    json += F("}");
    return json;
  }

  /**
   * \brief Универсальный парсер чисел с плавающей точкой из аргументов HTTP-запроса.
   */
  bool parse_float_arg(const __FlashStringHelper* argName,
                       float minValue,
                       float maxValue,
                       float currentValue,
                       float& outValue,
                       String& errorMsg) {
    const String name(argName);
    if (!gServer.hasArg(name)) {
      outValue = currentValue;
      return true;
    }

    String raw = gServer.arg(name);
    raw.trim();
    if (raw.length() == 0) {
      outValue = currentValue;
      return true;
    }

    char* endPtr = nullptr;
    const double parsed = strtod(raw.c_str(), &endPtr);
    if (endPtr == raw.c_str() || (endPtr && *endPtr != '\0') || !isfinite(parsed)) {
      errorMsg = String(F("Некорректное значение поля ")) + name;
      return false;
    }
    if (parsed < minValue || parsed > maxValue) {
      errorMsg = String(F("Поле ")) + name + F(" вне допустимого диапазона");
      return false;
    }
    outValue = static_cast<float>(parsed);
    return true;
  }

  /**
   * \brief Парсит целочисленное значение (long) из аргумента.
   */
  bool parse_long_arg(const __FlashStringHelper* argName,
                      long minValue,
                      long maxValue,
                      long currentValue,
                      long& outValue,
                      String& errorMsg) {
    const String name(argName);
    if (!gServer.hasArg(name)) {
      outValue = currentValue;
      return true;
    }

    String raw = gServer.arg(name);
    raw.trim();
    if (raw.length() == 0) {
      outValue = currentValue;
      return true;
    }

    char* endPtr = nullptr;
    const long parsed = strtol(raw.c_str(), &endPtr, 10);
    if (endPtr == raw.c_str() || (endPtr && *endPtr != '\0')) {
      errorMsg = String(F("Некорректное целое значение поля ")) + name;
      return false;
    }
    if (parsed < minValue || parsed > maxValue) {
      errorMsg = String(F("Поле ")) + name + F(" вне диапазона");
      return false;
    }
    outValue = parsed;
    return true;
  }

  /**
   * \brief Парсит беззнаковое значение (uint32_t) из аргумента.
   */
  bool parse_uint32_arg(const __FlashStringHelper* argName,
                        uint32_t minValue,
                        uint32_t maxValue,
                        uint32_t currentValue,
                        uint32_t& outValue,
                        String& errorMsg) {
    const String name(argName);
    if (!gServer.hasArg(name)) {
      outValue = currentValue;
      return true;
    }

    String raw = gServer.arg(name);
    raw.trim();
    if (raw.length() == 0) {
      outValue = currentValue;
      return true;
    }

    char* endPtr = nullptr;
    const unsigned long parsed = strtoul(raw.c_str(), &endPtr, 10);
    if (endPtr == raw.c_str() || (endPtr && *endPtr != '\0')) {
      errorMsg = String(F("Некорректное значение поля ")) + name;
      return false;
    }
    if (parsed < minValue || parsed > maxValue) {
      errorMsg = String(F("Поле ")) + name + F(" выходит за рамки допустимого");
      return false;
    }
    outValue = static_cast<uint32_t>(parsed);
    return true;
  }

  bool parse_bool_arg(const __FlashStringHelper* argName,
                      bool currentValue) {
    const String name(argName);
    if (!gServer.hasArg(name)) {
      return currentValue;
    }
    String raw = gServer.arg(name);
    raw.trim();
    raw.toLowerCase();
    if (raw == F("1") || raw == F("true") || raw == F("on") || raw == F("yes")) {
      return true;
    }
    if (raw == F("0") || raw == F("false") || raw == F("off") || raw == F("no")) {
      return false;
    }
    return currentValue;
  }

  /**
   * \brief Общий обработчик успешных ответов.
   */
  void send_ok_response(const String& message) {
    String json = F("{\"success\":true,\"message\":");
    json += '"';
    json += message;
    json += '"';
    json += '}';
    gServer.send(200, F("application/json"), json);
  }

  /**
   * \brief Общий обработчик ошибок.
   */
  void send_error_response(const String& message, int code = 400) {
    String json = F("{\"success\":false,\"message\":");
    json += '"';
    json += message;
    json += '"';
    json += '}';
    gServer.send(code, F("application/json"), json);
  }

  /**
   * \brief Кодирует произвольные бинарные данные в Base64 без зависимостей от ArduinoJson.
   */
  String base64_encode(const uint8_t* data, size_t length) {
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    String out;
    out.reserve(((length + 2) / 3) * 4);

    size_t i = 0;
    while (i + 2 < length) {
      const uint32_t chunk = (uint32_t(data[i]) << 16) |
                             (uint32_t(data[i + 1]) << 8) |
                             uint32_t(data[i + 2]);
      out += alphabet[(chunk >> 18) & 0x3F];
      out += alphabet[(chunk >> 12) & 0x3F];
      out += alphabet[(chunk >> 6) & 0x3F];
      out += alphabet[chunk & 0x3F];
      i += 3;
    }

    if (i < length) {
      uint32_t chunk = uint32_t(data[i]) << 16;
      out += alphabet[(chunk >> 18) & 0x3F];
      if (i + 1 < length) {
        chunk |= uint32_t(data[i + 1]) << 8;
        out += alphabet[(chunk >> 12) & 0x3F];
        out += alphabet[(chunk >> 6) & 0x3F];
        out += '=';
      } else {
        out += alphabet[(chunk >> 12) & 0x3F];
        out += '=';
        out += '=';
      }
    }

    return out;
  }

  /**
   * \brief Разбирает WebSocket-URL в составные части.
   */
  bool parse_ws_url(const std::string& url, WsEndpointParts& out) {
    out = WsEndpointParts{};
    if (url.empty()) {
      return false;
    }

    const size_t schemePos = url.find("://");
    if (schemePos == std::string::npos) {
      return false;
    }
    const std::string scheme = url.substr(0, schemePos);
    if (scheme == "ws") {
      out.secure = false;
    } else if (scheme == "wss") {
      out.secure = true;
    } else {
      return false;
    }

    const size_t authorityStart = schemePos + 3;
    if (authorityStart >= url.size()) {
      return false;
    }

    const size_t pathPos = url.find('/', authorityStart);
    std::string authority = (pathPos == std::string::npos)
                                ? url.substr(authorityStart)
                                : url.substr(authorityStart, pathPos - authorityStart);
    if (authority.empty()) {
      return false;
    }

    size_t colonPos = authority.find(':');
    std::string host = authority;
    uint16_t port = out.secure ? 443 : 80;
    if (colonPos != std::string::npos) {
      host = authority.substr(0, colonPos);
      const std::string portText = authority.substr(colonPos + 1);
      if (portText.empty()) {
        return false;
      }
      uint32_t accumulator = 0;
      for (char ch : portText) {
        if (!isdigit(static_cast<unsigned char>(ch))) {
          return false;
        }
        accumulator = accumulator * 10u + static_cast<uint32_t>(ch - '0');
        if (accumulator > 65535u) {
          return false;
        }
      }
      port = static_cast<uint16_t>(accumulator);
    }

    if (host.empty()) {
      return false;
    }

    std::string path = (pathPos == std::string::npos) ? std::string("/") : url.substr(pathPos);
    if (path.empty()) {
      path = "/";
    }

    out.host = host;
    out.port = port;
    out.path = path;
    return true;
  }

  /**
   * \brief Сериализует аудиокадр в компактный бинарный формат для WebSocket.
   */
  /**
   * \brief Формирует JSON с аудиокадром и диагностикой локализации.
   */
  String build_audio_chunk_json(const Audio::PcmChunk& chunk,
                                const Audio::Diagnostics& diag,
                                uint32_t sequence) {
    const size_t byteCount = chunk.interleaved.size() * sizeof(int16_t);
    const uint8_t* raw = reinterpret_cast<const uint8_t*>(chunk.interleaved.data());
    String payload = base64_encode(raw, byteCount);

    String json = F("{");
    json += F("\"sequence\":"); json += String(static_cast<unsigned long>(sequence));
    json += F(",\"sampleRate\":"); json += chunk.sampleRate;
    json += F(",\"channels\":"); json += chunk.channels;
    json += F(",\"timestampUs\":"); json += String(chunk.timestampUs);
    json += F(",\"directionDeg\":"); json += String(diag.directionDeg, 2);
    json += F(",\"confidence\":"); json += String(diag.confidence, 3);
    json += F(",\"localizationActive\":"); json += diag.localizationEnabled ? F("true") : F("false");
    json += F(",\"frameSamples\":"); json += String(diag.frameSamples);
    json += F(",\"micSpacingMeters\":"); json += String(diag.microphoneSpacingMeters, 3);
    json += F(",\"rmsLeft\":"); json += String(diag.rmsLeft, 4);
    json += F(",\"rmsRight\":"); json += String(diag.rmsRight, 4);
    json += F(",\"pcm16Base64\":\"");
    json += payload;
    json += '\"';
    json += '}';
    return json;
  }

  /**
   * \brief Парсинг duty из HTTP-аргумента.
   */
  int parse_duty_arg(const String& arg) {
    long value = arg.toInt();
    if (value < 0) value = 0;
    if (value > 1023) value = 1023;
    return static_cast<int>(value);
  }

  void handle_root() {
    // Подготавливаем HTML заранее, чтобы измерить вес ответа и записать это в отладочные логи.
    String page = render_root_page();
    gServer.send(200, F("text/html"), page);
#ifdef ARDUINO
    const bool audioUiEnabled = !gAudioStreamConfig.endpoint.empty();
    Serial.printf("[REMOTE] HTTP / отдан (%u байт, аудио=%s)\n",
                  static_cast<unsigned>(page.length()),
                  audioUiEnabled ? "active" : "disabled");
#endif
  }

  void handle_status() {
    gServer.send(200, F("application/json"), render_status_json());
  }

  void handle_params_get() {
    gServer.send(200, F("application/json"), render_config_json());
  }

  void handle_params_post() {
    if (gCommandInProgress) {
      send_error_response(F("Дождитесь завершения движения перед изменением настроек"), 409);
      return;
    }

    ConfigStore::TuningConfig cfg = ConfigStore::current_config();
    String error;

    float fval = cfg.wheel_diameter_mm;
    if (!parse_float_arg(F("wheelDiameterMm"), 20.0f, 300.0f, cfg.wheel_diameter_mm, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.wheel_diameter_mm = fval;

    if (!parse_float_arg(F("wheelBaseMm"), 80.0f, 600.0f, cfg.wheel_base_mm, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.wheel_base_mm = fval;

    long lval = cfg.tpr_left;
    if (!parse_long_arg(F("tprLeft"), 50, 50000, cfg.tpr_left, lval, error)) {
      send_error_response(error);
      return;
    }
    cfg.tpr_left = lval;

    if (!parse_long_arg(F("tprRight"), 50, 50000, cfg.tpr_right, lval, error)) {
      send_error_response(error);
      return;
    }
    cfg.tpr_right = lval;

    if (!parse_float_arg(F("kpStraightEnc"), 0.0f, 50.0f, cfg.kp_straight_enc, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.kp_straight_enc = fval;

    if (!parse_float_arg(F("kpStraightGyro"), 0.0f, 80.0f, cfg.kp_straight_gyro, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.kp_straight_gyro = fval;

    if (!parse_float_arg(F("kiStraightGyro"), 0.0f, 40.0f, cfg.ki_straight_gyro, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.ki_straight_gyro = fval;

    if (!parse_float_arg(F("kpTurnEnc"), 0.0f, 50.0f, cfg.kp_turn_enc, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.kp_turn_enc = fval;

    if (!parse_float_arg(F("kpTurnGyro"), 0.0f, 80.0f, cfg.kp_turn_gyro, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.kp_turn_gyro = fval;

    if (!parse_float_arg(F("kiTurnGyro"), 0.0f, 40.0f, cfg.ki_turn_gyro, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.ki_turn_gyro = fval;

    if (!parse_float_arg(F("headingToleranceDeg"), 0.0f, 15.0f, cfg.heading_tolerance_deg, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.heading_tolerance_deg = fval;

    if (!parse_float_arg(F("stuckRateThresholdDps"), 0.0f, 90.0f, cfg.stuck_rate_threshold_dps, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.stuck_rate_threshold_dps = fval;

    uint32_t uval = cfg.stuck_timeout_ms;
    if (!parse_uint32_arg(F("stuckTimeoutMs"), 50, 10000, cfg.stuck_timeout_ms, uval, error)) {
      send_error_response(error);
      return;
    }
    cfg.stuck_timeout_ms = uval;

    if (!parse_float_arg(F("yawIntegralLimit"), 0.0f, 400.0f, cfg.yaw_integral_limit, fval, error)) {
      send_error_response(error);
      return;
    }
    cfg.yaw_integral_limit = fval;

    cfg.enable_gyro_feedback = parse_bool_arg(F("enableGyro"), cfg.enable_gyro_feedback);
    cfg.use_gyro_for_straight = parse_bool_arg(F("useGyroStraight"), cfg.use_gyro_for_straight);

    if (!ConfigStore::update_and_persist(cfg)) {
      send_error_response(F("Не удалось сохранить параметры"), 500);
      return;
    }

    Serial.println(F("[REMOTE] параметры движения обновлены и сохранены"));
    send_ok_response(F("Параметры движения сохранены"));
  }

  void handle_move() {
    const String directionArg = gServer.arg(F("direction"));
    const String distanceArg  = gServer.arg(F("distance"));
    const String dutyArg      = gServer.arg(F("duty"));

    if (distanceArg.isEmpty()) {
      send_error_response(F("Не указано расстояние"));
      return;
    }

    const float distance = distanceArg.toFloat();
    if (distance <= 0.0f) {
      send_error_response(F("Расстояние должно быть положительным"));
      return;
    }

    Command cmd;
    cmd.action = Action::Move;
    cmd.direction = (directionArg == F("backward")) ? Direction::Backward : Direction::Forward;
    cmd.value = distance;
    cmd.duty = parse_duty_arg(dutyArg);

    if (!push_command(cmd)) {
      send_error_response(F("Команда уже выполняется"), 409);
      return;
    }

    String msg = F("Движение поставлено в очередь: ");
    msg += describe_command(cmd);
    send_ok_response(msg);
  }

  void handle_rotate() {
    const String directionArg = gServer.arg(F("direction"));
    const String angleArg     = gServer.arg(F("angle"));
    const String dutyArg      = gServer.arg(F("duty"));

    if (angleArg.isEmpty()) {
      send_error_response(F("Не указан угол"));
      return;
    }

    const float angle = angleArg.toFloat();
    if (angle <= 0.0f) {
      send_error_response(F("Угол должен быть положительным"));
      return;
    }

    Command cmd;
    cmd.action = Action::Rotate;
    cmd.direction = (directionArg == F("right")) ? Direction::Backward : Direction::Forward;
    cmd.value = angle;
    cmd.duty = parse_duty_arg(dutyArg);

    if (!push_command(cmd)) {
      send_error_response(F("Команда уже выполняется"), 409);
      return;
    }

    String msg = F("Поворот поставлен в очередь: ");
    msg += describe_command(cmd);
    send_ok_response(msg);
  }

  void handle_stop() {
    Command cmd;
    cmd.action = Action::EmergencyStop;
    if (!push_command(cmd)) {
      // Если очередь занята, всё равно принудительно очищаем и добавляем стоп.
      clear_commands();
      push_command(cmd);
    }
    send_ok_response(F("Аварийная остановка отправлена"));
  }

  void handle_audio_chunk() {
    Audio::PcmChunk chunk;
    if (!Audio::pop_chunk(chunk)) {
      gServer.send(204, F("application/json"), F("{\"success\":false,\"message\":\"no-audio\"}"));
      return;
    }

    const Audio::Diagnostics diag = Audio::latest_diagnostics();
    const auto streamStats = snapshot_audio_stream_stats();
    String payload = build_audio_chunk_json(chunk, diag, streamStats.nextSequence);
    payload.remove(0, 1); // удаляем начальную '{', чтобы встроить success на верхнем уровне

    String json = F("{\"success\":true,");
    json += payload;

    gServer.send(200, F("application/json"), json);
#ifdef ARDUINO
    const size_t byteCount = chunk.interleaved.size() * sizeof(int16_t);
    if (diag.localizationEnabled) {
      Serial.printf("[AUDIO] HTTP chunk: %u bytes (dir=%.1f° conf=%.2f)\n",
                    static_cast<unsigned>(byteCount),
                    diag.directionDeg,
                    diag.confidence);
    } else {
      Serial.printf("[AUDIO] HTTP chunk: %u bytes (rms=%.3f/%.3f)\n",
                    static_cast<unsigned>(byteCount),
                    diag.rmsLeft,
                    diag.rmsRight);
    }
#endif
  }

#ifdef ARDUINO

  void note_queue_drop(const char* reason) {
    lock_stream_stats();
    gAudioStreamStats.queueDrops++;
    gAudioStreamStats.framesFailed++;
    gAudioStreamStats.lastAttemptOk = false;
    gAudioStreamStats.lastDurationMs = 0;
    gAudioStreamStats.lastAttemptMs = static_cast<uint64_t>(millis());
    gAudioStreamStats.lastError = reason ? reason : "queue-drop";
    unlock_stream_stats();
    mark_status_dirty();
  }

  bool stream_queue_has_capacity(unsigned long nowMs) {
    if (!gStreamQueue) {
      return true;
    }

    const UBaseType_t spaces = uxQueueSpacesAvailable(gStreamQueue);
    if (spaces > 0) {
      return true;
    }

    lock_stream_stats();
    gAudioStreamStats.queueStalls++;
    unlock_stream_stats();

    if (nowMs - gLastQueueSaturationLogMs > 1000UL) {
      Serial.println(F("[AUDIO] очередь аудиопотока заполнена, ожидаем освобождение"));
      gLastQueueSaturationLogMs = nowMs;
    }
    return false;
  }

  bool ensure_stream_task_running();
  void audio_stream_task(void*);

  void flush_stream_queue() {
    if (!gStreamQueue) {
      return;
    }
    StreamWorkItem* stale = nullptr;
    while (xQueueReceive(gStreamQueue, &stale, 0) == pdPASS) {
      delete stale;
    }
    // Обновляем метрики очереди, не вызывая loop() клиента здесь, чтобы не зависать основной цикл.
    update_queue_depth_metric();
  }

  void apply_send_result(const StreamWorkItem& item,
                         bool success,
                         unsigned long attemptMs,
                         unsigned long durationMs,
                         const std::string& errorText) {
    lock_stream_stats();
    gAudioStreamStats.lastAttemptMs = static_cast<uint64_t>(attemptMs);
    gAudioStreamStats.lastDurationMs = durationMs;
    gAudioStreamStats.lastAttemptOk = success;
    if (success) {
      gAudioStreamStats.framesSent++;
      gAudioStreamStats.bytesSent += static_cast<uint64_t>(item.payload.size());
      gAudioStreamStats.lastError.clear();
    } else {
      gAudioStreamStats.framesFailed++;
      gAudioStreamStats.lastError = errorText.empty() ? std::string("unknown") : errorText;
    }
    unlock_stream_stats();
    mark_status_dirty();
  }

  void handle_websocket_event(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
      case WStype_CONNECTED: {
        const char* info = (payload && length > 0) ? reinterpret_cast<const char*>(payload) : "";
        Serial.printf("[AUDIO] WebSocket подключён: %s\n", info);
        gLastHandshakeMs = millis();
        gLastWsWaitLogMs = 0;
        lock_stream_stats();
        gAudioStreamStats.wsConnected = true;
        gAudioStreamStats.wsReconnects++;
        gAudioStreamStats.wsLastConnectMs = static_cast<uint64_t>(millis());
        gAudioStreamStats.lastError.clear();
        unlock_stream_stats();
        mark_status_dirty();
        break;
      }
      case WStype_DISCONNECTED:
        Serial.println(F("[AUDIO] WebSocket отключён, инициируем переподключение"));
        gLastHandshakeMs = millis();
        // При любом разрыве очищаем очередь фоновой отправки, чтобы старые кадры
        // не мешали новой сессии и не вызывали ложных переполнений.
        flush_stream_queue();
        lock_stream_stats();
        gAudioStreamStats.wsConnected = false;
        gAudioStreamStats.wsLastDisconnectMs = static_cast<uint64_t>(millis());
        if (gAudioStreamStats.lastError.empty()) {
          gAudioStreamStats.lastError = "ws-disconnected";
        }
        unlock_stream_stats();
        mark_status_dirty();
        // Полностью сбрасываем состояние клиента, чтобы следующая попытка
        // подключения стартовала «с нуля» и не зависела от внутреннего
        // состояния библиотеки после аварийного закрытия (код 1006 и подобные).
        reset_websocket_client_state("ws-disconnected");
        break;
      case WStype_ERROR:
        Serial.println(F("[AUDIO] ошибка WebSocket, сбрасываем клиент"));
        gLastHandshakeMs = millis();
        lock_stream_stats();
        gAudioStreamStats.wsConnected = false;
        gAudioStreamStats.lastError = "ws-error";
        unlock_stream_stats();
        mark_status_dirty();
        flush_stream_queue();
        reset_websocket_client_state("ws-error");
        break;
      case WStype_BIN: {
        const bool accepted = AudioPlayback::feed_stream_chunk(payload, length);
        if (!accepted) {
          Serial.println(F("[PLAYBACK] предупреждение: сервер прислал PCM, который не удалось воспроизвести"));
        }
        break;
      }
      case WStype_TEXT:
        if (payload && length > 0) {
#ifdef ARDUINO
          DynamicJsonDocument doc(256);
          const DeserializationError err = deserializeJson(doc, payload, length);
          if (err) {
            Serial.printf("[AUDIO] ошибка разбора JSON от сервера: %s\n", err.c_str());
            break;
          }
          const char* msgType = doc["type"] | "";
          if (strcmp(msgType, "audio_start") == 0) {
            const uint32_t sr = doc["sample_rate"] | 16000;
            const uint8_t ch = doc["channels"] | 1;
            const float volume = doc["volume"] | 1.0f;
            if (AudioPlayback::start_stream(sr, ch, volume)) {
              pause_mic_stream("воспроизведение начато");
            }
          } else if (strcmp(msgType, "audio_end") == 0) {
            AudioPlayback::stop_stream("audio_end");
            resume_mic_stream("воспроизведение завершено");
          } else if (strcmp(msgType, "emotion") == 0) {
            const char* val = doc["value"] | "";
            Serial.printf("[AUDIO] эмоция сервера: %s\n", val);
          } else {
            Serial.printf("[AUDIO] текстовое сообщение от сервера: %.*s\n",
                          static_cast<int>(length),
                          reinterpret_cast<const char*>(payload));
          }
#else
          const std::string text(reinterpret_cast<const char*>(payload), length);
          if (text.find("audio_start") != std::string::npos) {
            AudioPlayback::start_stream(16000, 1, 1.0f);
            pause_mic_stream("audio_start");
          } else if (text.find("audio_end") != std::string::npos) {
            AudioPlayback::stop_stream("audio_end");
            resume_mic_stream("audio_end");
          }
          std::printf("[AUDIO] текстовое сообщение от сервера: %s\n", text.c_str());
#endif
        }
        break;
      case WStype_PONG:
        lock_stream_stats();
        gAudioStreamStats.lastAttemptMs = static_cast<uint64_t>(millis());
        unlock_stream_stats();
        mark_status_dirty();
        break;
      default:
        break;
    }
  }

  void configure_websocket_headers(const AudioStreamConfig& cfg) {
    gWsExtraHeaders.clear();
    if (!cfg.authHeader.empty()) {
      gWsExtraHeaders += "Authorization: ";
      gWsExtraHeaders += cfg.authHeader;
      gWsExtraHeaders += "\r\n";
    }
    if (!cfg.subprotocol.empty()) {
      gWsExtraHeaders += "Sec-WebSocket-Protocol: ";
      gWsExtraHeaders += cfg.subprotocol;
      gWsExtraHeaders += "\r\n";
    }
    if (!gWsExtraHeaders.empty()) {
      gWebsocketClient.setExtraHeaders(gWsExtraHeaders.c_str());
    } else {
      gWebsocketClient.setExtraHeaders(nullptr);
    }
  }

  void reset_websocket_client_state(const char* reason) {
    const unsigned long now = millis();
    if (reason && *reason) {
      Serial.printf("[AUDIO] перезапуск WebSocket из-за: %s\n", reason);
    } else {
      Serial.println(F("[AUDIO] перезапуск WebSocket по запросу"));
    }

    // Полностью очищаем состояние клиента, чтобы следующая попытка рукопожатия стартовала с нуля.
    gWebsocketClient.disconnect();
    gWebsocketConfigured = false;
    gHasActiveWsConfig = false;
    gActiveWsConfig = AudioStreamConfig{};
    gLastWsUrl.clear();
    gWsExtraHeaders.clear();
    gLastHandshakeMs = now;
    gLastWsWaitLogMs = 0;

    lock_stream_stats();
    gAudioStreamStats.wsConnected = false;
    gAudioStreamStats.lastError = reason ? reason : std::string("ws-reset");
    unlock_stream_stats();
    mark_status_dirty();
  }

  bool configure_websocket_client(const AudioStreamConfig& cfg, std::string& errorText) {
    if (cfg.endpoint.empty()) {
      errorText = "disabled";
      return false;
    }

    const bool configChanged = !gHasActiveWsConfig ||
                               cfg.endpoint != gActiveWsConfig.endpoint ||
                               cfg.authHeader != gActiveWsConfig.authHeader ||
                               cfg.subprotocol != gActiveWsConfig.subprotocol ||
                               cfg.reconnectIntervalMs != gActiveWsConfig.reconnectIntervalMs ||
                               cfg.pingIntervalMs != gActiveWsConfig.pingIntervalMs;

    if (configChanged || !gWebsocketConfigured) {
      WsEndpointParts parts;
      if (!parse_ws_url(cfg.endpoint, parts)) {
        errorText = "bad-url";
        return false;
      }

      gWebsocketClient.onEvent(handle_websocket_event);
      gWebsocketClient.setReconnectInterval(cfg.reconnectIntervalMs);
      if (cfg.pingIntervalMs > 0) {
        gWebsocketClient.enableHeartbeat(cfg.pingIntervalMs, cfg.pingIntervalMs, 2);
      }

      configure_websocket_headers(cfg);

      if (parts.secure) {
        gWebsocketClient.beginSSL(parts.host.c_str(), parts.port, parts.path.c_str());
      } else {
        gWebsocketClient.begin(parts.host.c_str(), parts.port, parts.path.c_str());
      }

      Serial.printf("[AUDIO] WebSocket подключение: %s:%u%s\n",
                    parts.host.c_str(),
                    static_cast<unsigned>(parts.port),
                    parts.path.c_str());

      gWebsocketConfigured = true;
      gActiveWsConfig = cfg;
      gHasActiveWsConfig = true;
      gLastWsUrl = cfg.endpoint;
      // Фиксируем момент старта нового рукопожатия, чтобы корректно отслеживать таймаут ожидания сервера.
      gLastHandshakeMs = millis();
      gLastWsWaitLogMs = 0;
    }

    return true;
  }

  bool websocket_ready_for_send(const AudioStreamConfig& cfg, std::string& errorText) {
    if (!configure_websocket_client(cfg, errorText)) {
      return false;
    }

    if (gWebsocketClient.isConnected()) {
      return true;
    }

    const unsigned long now = millis();
    const unsigned long elapsed = (now >= gLastHandshakeMs) ? (now - gLastHandshakeMs) : 0UL;
    if (detail::handshake_timeout_elapsed(cfg.handshakeTimeoutMs, elapsed)) {
      errorText = "ws-timeout";

      // Фиксируем таймаут в статистике ещё до сброса клиента, чтобы оператор видел причину отставания.
      lock_stream_stats();
      gAudioStreamStats.wsTimeouts++;
      gAudioStreamStats.wsConnected = false;
      gAudioStreamStats.lastError = errorText;
      unlock_stream_stats();
      mark_status_dirty();

      reset_websocket_client_state(errorText.c_str());
    }

    return false;
  }

  bool ensure_stream_task_running() {
    if (!gStreamQueue) {
      gStreamQueue = xQueueCreate(AUDIO_STREAM_QUEUE_DEPTH, sizeof(StreamWorkItem*));
      if (!gStreamQueue) {
        Serial.println(F("[AUDIO] критическая ошибка: не удалось создать очередь аудио"));
        note_queue_drop("queue-create");
        return false;
      }
      update_queue_depth_metric();
    }

    if (!gStreamTask) {
      const BaseType_t rc = xTaskCreatePinnedToCore(audio_stream_task,
                                                    "audio_stream",
                                                    AUDIO_STREAM_TASK_STACK_WORDS,
                                                    nullptr,
                                                    AUDIO_STREAM_TASK_PRIORITY,
                                                    &gStreamTask,
                                                    1);
      if (rc != pdPASS) {
        Serial.println(F("[AUDIO] критическая ошибка: не удалось создать задачу аудио"));
        note_queue_drop("task-create");
        return false;
      }
      Serial.println(F("[AUDIO] фоновая задача аудиопотока запущена"));
    }

    return true;
  }

  void audio_stream_task(void*) {
    Serial.println(F("[AUDIO] задача отправки звука активирована"));
    for (;;) {
      // Ежитерационно обслуживаем WebSocket, чтобы даже при пустой очереди поддерживалось подключение
      // и библиотека могла выполнять TCP-рукопожатие без блокировки главного loop().
      gWebsocketClient.loop();

      StreamWorkItem* item = nullptr;
      if (xQueueReceive(gStreamQueue, &item, pdMS_TO_TICKS(50)) != pdPASS) {
        // Повторно вызываем loop() после таймаута ожидания, чтобы поддерживать HeartBeat даже без кадров.
        gWebsocketClient.loop();
        continue;
      }
      if (!item) {
        gWebsocketClient.loop();
        continue;
      }

      update_queue_depth_metric();

      const auto cfg = snapshot_audio_stream_config();
      const unsigned long attemptStart = millis();
      unsigned long durationMs = 0;
      bool success = false;
      std::string error;

      if (cfg.endpoint.empty()) {
        error = "disabled";
      } else if (WiFi.status() != WL_CONNECTED) {
        error = "wifi-down";
      } else {
        while (true) {
          std::string configError;
          if (!websocket_ready_for_send(cfg, configError)) {
            gWebsocketClient.loop();
            if (!configError.empty()) {
              error = configError;
            } else {
              const unsigned long nowMs = millis();
              if (nowMs - gLastWsWaitLogMs > 1000UL) {
                Serial.println(F("[AUDIO] WebSocket недоступен, кадр отброшен"));
                gLastWsWaitLogMs = nowMs;
              }

              // Увеличиваем счётчик отброшенных кадров и сообщаем интерфейсу, чтобы оператор видел потерю.
              lock_stream_stats();
              gAudioStreamStats.wsOfflineDrops++;
              unlock_stream_stats();
              mark_status_dirty();

              error = "ws-offline";
            }
            break;
          }

          const unsigned long sendStart = millis();
          if (!gWebsocketClient.sendBIN(item->payload.data(), item->payload.size())) {
            error = "ws-send";
          } else {
            durationMs = millis() - sendStart;
            success = true;
          }
          gWebsocketClient.loop();
          break;
        }
      }

      if (!item) {
        vTaskDelay(pdMS_TO_TICKS(20));
        gWebsocketClient.loop();
        continue;
      }

      apply_send_result(*item, success, attemptStart, durationMs, error);

      if (!success && error == "ws-offline") {
        // Короткая пауза защищает остальные задачи от агрессивного цикла при отсутствующем сервере.
        vTaskDelay(pdMS_TO_TICKS(20));
      }

      const auto statsSnapshot = snapshot_audio_stream_stats();
      if (success) {
        Serial.printf("[AUDIO] кадр #%u отправлен (ws, %lums, payload=%uB queue=%u)\n",
                      static_cast<unsigned>(item->sequence),
                      durationMs,
                      static_cast<unsigned>(item->payload.size()),
                      static_cast<unsigned>(statsSnapshot.queueDepth));
      } else {
        Serial.printf("[AUDIO] ошибка передачи #%u: %s\n",
                      static_cast<unsigned>(item->sequence),
                      error.empty() ? "unknown" : error.c_str());
      }

      delete item;
    }
  }

  void process_audio_streaming(unsigned long now) {
    const auto cfg = snapshot_audio_stream_config();
    if (cfg.endpoint.empty()) {
      return;
    }

    if (!ensure_stream_task_running()) {
      return;
    }

    // Основной поток не вызывает loop() клиента, чтобы исключить паузы в обработке HTTP/WebSocket-управления.
    update_queue_depth_metric();
    if (!stream_queue_has_capacity(now)) {
      return;
    }

    if (gMicPausedForPlayback) {
      if (now - gLastMicPauseLogMs > 1000UL) {
#ifdef ARDUINO
        Serial.println(F("[AUDIO] микрофон в паузе из-за проигрывания TTS"));
#endif
        gLastMicPauseLogMs = now;
      }
      return;
    }

    Audio::PcmChunk chunk;
    if (!Audio::pop_chunk(chunk)) {
      return;
    }

    const Audio::Diagnostics diag = Audio::latest_diagnostics();

    StreamWorkItem* item = new (std::nothrow) StreamWorkItem();
    if (!item) {
      Serial.println(F("[AUDIO] ошибка: недостаточно памяти для очереди аудио"));
      note_queue_drop("oom");
      return;
    }

    lock_stream_stats();
    item->sequence = gAudioStreamStats.nextSequence++;
    unlock_stream_stats();

    item->payload = build_audio_stream_frame(chunk, diag, item->sequence);
    item->pcmBytes = chunk.interleaved.size() * sizeof(int16_t);
    item->diag = diag;
    item->enqueueMs = now;

    if (xQueueSend(gStreamQueue, &item, 0) != pdPASS) {
      delete item;
      note_queue_drop("queue-full");
      update_queue_depth_metric();
      Serial.println(F("[AUDIO] предупреждение: очередь аудиопотока переполнена, кадр отброшен"));
      return;
    }

    update_queue_depth_metric();
    const auto statsSnapshot = snapshot_audio_stream_stats();
    Serial.printf("[AUDIO] кадр #%u поставлен в очередь (payload=%uB depth=%u)\n",
                  static_cast<unsigned>(item->sequence),
                  static_cast<unsigned>(item->payload.size()),
                  static_cast<unsigned>(statsSnapshot.queueDepth));
  }

#endif

#endif // ARDUINO

#ifdef ARDUINO
  static void log_stream_config_change() {
    const auto cfg = snapshot_audio_stream_config();
    if (cfg.endpoint.empty()) {
      Serial.println(F("[AUDIO] поток аудио отключён настройками"));
    } else {
      Serial.printf("[AUDIO] поток аудио настроен: %s (reconnect %ums, ping %ums)\n",
                    cfg.endpoint.c_str(),
                    static_cast<unsigned>(cfg.reconnectIntervalMs),
                    static_cast<unsigned>(cfg.pingIntervalMs));
    }
  }
#endif

} // namespace

bool push_command(const Command& cmd) {
  if (cmd.action == Action::EmergencyStop) {
    // --- Аварийная остановка всегда должна иметь возможность попасть в очередь ---
    gPendingCommand = cmd;
    gHasPendingCommand = true;
    gCommandInProgress = false; // Разрешаем немедленно возобновить телеметрию после обработки.
#ifdef ARDUINO
    Serial.println(F("[REMOTE] аварийная остановка принята вне очереди"));
#endif
    return true;
  }

  if (gHasPendingCommand || gCommandInProgress) {
#ifdef ARDUINO
    Serial.println(F("[REMOTE] команда отклонена: очередь или исполнитель заняты"));
#endif
    return false;
  }

  gPendingCommand = cmd;
  gHasPendingCommand = true;
#ifdef ARDUINO
  Serial.printf("[REMOTE] новая команда: %s\n", describe_command(cmd).c_str());
#endif
  return true;
}

bool fetch_command(Command& out) {
  if (!gHasPendingCommand) {
    return false;
  }
  out = gPendingCommand;
  gHasPendingCommand = false;
  gCommandInProgress = (out.action != Action::EmergencyStop);
  return true;
}

void clear_commands() {
  gHasPendingCommand = false;
  gCommandInProgress = false;
}

void update_diagnostics(const Diagnostics& diag) {
  gDiagnostics = diag;
#ifdef ARDUINO
  gDiagnostics.batteryPercent = estimate_battery_percent(diag.busVoltage);
#endif
  mark_status_dirty();
}

void notify_command_complete() {
  // --- После завершения движения разблокируем очередь и телеметрию ---
  gCommandInProgress = false;
#ifdef ARDUINO
  Serial.println(F("[REMOTE] движение завершено и очередь разблокирована"));
#endif
}

bool is_busy() {
  return gHasPendingCommand || gCommandInProgress;
}

bool telemetry_updates_allowed() {
  // Подробный комментарий: когда робот выполняет команду из веб-интерфейса,
  // энкодеры не должны сбрасываться подсистемой телеметрии. Иначе мы обнулим
  // счётчики прямо во время движения и основная логика одометрии потеряет
  // прогресс, продолжая крутить колёса до бесконечности. Поэтому, пока
  // `gCommandInProgress == true`, запрещаем периодическим опросам очищать
  // тики. Снаружи это выражается как временное отключение обновлений для
  // подсистем, которым требуется «свежий» `enc_get_and_clear`.
  return !gCommandInProgress;
}

#ifndef ARDUINO

void init(const char*, const char*) {}
void loop() {}

#else

void init(const char* ssid, const char* password) {
  Serial.println(F("[REMOTE] Инициализация Wi-Fi для удалённого управления"));
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, password);

  unsigned long start = millis();
  const unsigned long timeoutMs = 20000; // 20 секунд на подключение.
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < timeoutMs) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[REMOTE] Wi-Fi подключён: SSID=%s IP=%s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  } else {
    Serial.println(F("[REMOTE] Не удалось подключиться к Wi-Fi. Интерфейс останется недоступным."));
    return;
  }

  gServer.on(F("/"), handle_root);
  gServer.on(F("/api/status"), handle_status);
  gServer.on(F("/api/move"), handle_move);
  gServer.on(F("/api/rotate"), handle_rotate);
  gServer.on(F("/api/stop"), handle_stop);
  gServer.on(F("/api/params"), HTTP_GET, handle_params_get);
  gServer.on(F("/api/params"), HTTP_POST, handle_params_post);
  gServer.on(F("/api/audio/chunk"), HTTP_GET, handle_audio_chunk);
  gServer.begin();
  Serial.println(F("[REMOTE] Веб-сервер запущен на порту 80"));

  gTelemetryWs.begin();
  gTelemetryWs.onEvent(telemetry_ws_event);
  gTelemetryWs.enableHeartbeat(TELEMETRY_WS_HEARTBEAT_INTERVAL_MS,
                               TELEMETRY_WS_HEARTBEAT_TIMEOUT_MS,
                               TELEMETRY_WS_HEARTBEAT_FAILURES);
  gTelemetryWsStarted = true;
  Serial.printf("[TELEM] WebSocket телеметрии запущен на порту %u\n",
                static_cast<unsigned>(TELEMETRY_WS_PORT));
  mark_status_dirty();
}

void loop() {
  gServer.handleClient();

  if (gTelemetryWsStarted) {
    gTelemetryWs.loop();
  }

  const unsigned long now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (now - gLastWifiLog > 2000) {
      Serial.println(F("[REMOTE] предупреждение: Wi-Fi отключён"));
      gLastWifiLog = now;
    }
  } else if (now - gLastWifiLog > 10000) {
    Serial.printf("[REMOTE] Wi-Fi активен: RSSI=%d dBm IP=%s\n", WiFi.RSSI(), WiFi.localIP().toString().c_str());
    gLastWifiLog = now;
  }

  process_audio_streaming(now);
  emit_status_over_ws(false);
}

#endif // ARDUINO

void set_audio_stream_config(const AudioStreamConfig& cfg) {
  lock_stream_config();
  gAudioStreamConfig = cfg;
  unlock_stream_config();

  AudioStreamStats resetStats{};
  if (cfg.endpoint.empty()) {
    resetStats.lastError = "disabled";
    resetStats.wsConnected = false;
  }
  overwrite_audio_stream_stats(resetStats);
  mark_status_dirty();

#ifdef ARDUINO
  gLastQueueSaturationLogMs = 0; // Сбрасываем таймер предупреждений при смене конфигурации.
  if (cfg.endpoint.empty()) {
    flush_stream_queue();
    gWebsocketClient.disconnect();
    gWebsocketConfigured = false;
    gHasActiveWsConfig = false;
    gActiveWsConfig = AudioStreamConfig{};
    gLastWsUrl.clear();
    gWsExtraHeaders.clear();
  } else {
    ensure_stream_task_running();
  }
  log_stream_config_change();
#endif
}

AudioStreamConfig audio_stream_config() {
  return snapshot_audio_stream_config();
}

AudioStreamStats audio_stream_stats() {
  return snapshot_audio_stream_stats();
}

TelemetryStreamStats telemetry_stream_stats() {
  return snapshot_telemetry_stats();
}

std::vector<uint8_t> build_audio_stream_frame(const Audio::PcmChunk& chunk,
                                              const Audio::Diagnostics& diag,
                                              uint32_t sequence) {
  const size_t pcmBytes = chunk.interleaved.size() * sizeof(int16_t);
  const uint32_t frameSamples = diag.frameSamples != 0
                                   ? diag.frameSamples
                                   : (chunk.channels > 0 ?
                                          static_cast<uint32_t>(chunk.interleaved.size() / chunk.channels)
                                          : 0u);

  std::vector<uint8_t> payload;
  payload.reserve(64 + pcmBytes);

  auto append_u8 = [&payload](uint8_t value) { payload.push_back(value); };
  auto append_u16 = [&payload](uint16_t value) {
    payload.push_back(static_cast<uint8_t>(value & 0xFF));
    payload.push_back(static_cast<uint8_t>((value >> 8) & 0xFF));
  };
  auto append_u32 = [&payload](uint32_t value) {
    payload.push_back(static_cast<uint8_t>(value & 0xFF));
    payload.push_back(static_cast<uint8_t>((value >> 8) & 0xFF));
    payload.push_back(static_cast<uint8_t>((value >> 16) & 0xFF));
    payload.push_back(static_cast<uint8_t>((value >> 24) & 0xFF));
  };
  auto append_u64 = [&payload](uint64_t value) {
    for (int i = 0; i < 8; ++i) {
      payload.push_back(static_cast<uint8_t>((value >> (8 * i)) & 0xFF));
    }
  };
  auto append_f32 = [&payload, &append_u32](float value) {
    static_assert(sizeof(float) == sizeof(uint32_t), "IEEE754 float assumed");
    uint32_t raw;
    std::memcpy(&raw, &value, sizeof(raw));
    append_u32(raw);
  };

  append_u8('A');
  append_u8('F');
  append_u8(1);
  uint8_t flags = 0;
  if (diag.localizationEnabled) {
    flags |= 0x01;
  }
  append_u8(flags);
  append_u32(sequence);
  append_u64(chunk.timestampUs);
  append_u32(chunk.sampleRate);
  append_u32(frameSamples);
  append_u16(chunk.channels);
  append_u16(16);
  append_u32(static_cast<uint32_t>(pcmBytes));
  append_f32(diag.rmsLeft);
  append_f32(diag.rmsRight);
  append_f32(diag.microphoneSpacingMeters);
  append_f32(diag.directionDeg);
  append_f32(diag.confidence);

  if (!chunk.interleaved.empty()) {
    const uint8_t* raw = reinterpret_cast<const uint8_t*>(chunk.interleaved.data());
    payload.insert(payload.end(), raw, raw + pcmBytes);
  }

  return payload;
}

#ifndef ARDUINO
#endif

} // namespace RemoteControl
