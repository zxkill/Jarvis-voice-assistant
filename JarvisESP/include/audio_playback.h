#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace AudioPlayback {

/**
 * \brief Режим вывода аудиосигнала.
 */
enum class OutputMode : uint8_t {
  InternalDac = 0, ///< Использовать встроенный ЦАП ESP32 (моно канал DAC1 на GPIO25).
  ExternalI2S = 1, ///< Использовать внешний I2S-усилитель (например, MAX98357A) через выводы BCLK/LRCLK/DIN.
};

/**
 * \brief Конфигурация приёмника аудиопотока от сервера.
 */
struct Config {
  OutputMode mode = OutputMode::ExternalI2S; ///< Каким способом выводить звук (по умолчанию — внешний I2S-усилитель).
  int pinBclk = 26;                          ///< Пин I2S BCLK для внешнего усилителя (по умолчанию GPIO26 под MAX98357A).
  int pinWs = 27;                            ///< Пин I2S LRCLK/WS (по умолчанию GPIO27 под MAX98357A).
  int pinData = 25;                          ///< Пин I2S DIN (по умолчанию GPIO25 под MAX98357A).
  uint32_t defaultSampleRate = 16000;        ///< Частота дискретизации по умолчанию, Гц.
  size_t frameSamplesHint = 512;             ///< Оценка длины кадра для расчёта буферов.
  size_t queueCapacity = 6;                  ///< Максимальное количество кадров в очереди.
  float defaultVolume = 1.0f;                ///< Усиление, применяемое при отсутствии указания в кадре.
  uint32_t idleMuteDelayMs = 250;            ///< Задержка (мс) без аудиокадров, после которой тракт переводится в режим тишины.
};

/**
 * \brief Раскодированный кадр, полученный от сервера.
 */
struct Frame {
  uint32_t sequence = 0;        ///< Порядковый номер чанка внутри текущей сессии.
  uint32_t sampleRate = 0;      ///< Частота дискретизации, Гц.
  uint16_t channels = 0;        ///< Количество каналов (1 или 2).
  float volume = 1.0f;          ///< Нормированное усиление (1.0 = без изменений).
  std::vector<int16_t> samples; ///< Интерливированный PCM16 (L,R,L,R ...).
};

/**
 * \brief Диагностика приёмника аудио.
 */
struct Stats {
  uint32_t chunksAccepted = 0;     ///< Сколько бинарных чанков успешно поставлено в очередь воспроизведения.
  uint32_t chunksRejected = 0;     ///< Сколько чанков отброшено из-за ошибок состояния или формата.
  uint32_t chunksPlayed = 0;       ///< Сколько чанков реально дошло до ЦАП.
  uint32_t queueDrops = 0;         ///< Сколько чанков потеряно из-за переполнения очереди.
  uint32_t bufferUnderruns = 0;    ///< Сколько раз поток иссякал до окончания воспроизведения.
  uint32_t silencePrimed = 0;      ///< Сколько раз буфер ЦАП заполнялся «тишиной» для устранения фонового треска.
  uint32_t lastSequence = 0;       ///< Последний принятый номер чанка.
  uint32_t lastSampleRate = 0;     ///< Последняя частота дискретизации, применённая к ЦАП.
  uint32_t queueDepth = 0;         ///< Текущая глубина очереди.
  uint32_t queueHighWatermark = 0; ///< Максимальная глубина очереди со старта.
  float lastVolume = 0.0f;         ///< Фактически применённая громкость последнего чанка.
  bool initialized = false;        ///< Успешно ли инициализирован вывод звука.
  uint32_t idleTransitions = 0;    ///< Сколько раз тракт переходил в режим программного «мьюта».
  bool muted = false;              ///< Находится ли тракт сейчас в режиме тишины (I2S/DAC остановлены).
  std::string lastError;           ///< Последняя текстовая ошибка.
};

/**
 * \brief Инициализирует приёмник аудиопотока и запускает фоновые службы воспроизведения.
 */
bool init(const Config& cfg);

/**
 * \brief Останавливает воспроизведение и освобождает ресурсы.
 */
void shutdown();

/**
 * \brief Сбрасывает диагностические счётчики.
 */
void reset_stats();

/**
 * \brief Возвращает копию текущей статистики.
 */
Stats stats();

/**
 * \brief Обрабатывает бинарный кадр, полученный от сервера через WebSocket.
 * \return true, если кадр принят в очередь воспроизведения.
 */
bool start_stream(uint32_t sampleRate, uint8_t channels, float volume = 1.0f);

bool feed_stream_chunk(const uint8_t* payload, size_t length);

void stop_stream(const char* reason = nullptr);

} // namespace AudioPlayback

