#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace AudioPlayback {

/**
 * \brief Режим вывода аудиосигнала.
 */
enum class OutputMode : uint8_t {
  InternalDac = 0, ///< Использовать встроенный ЦАП ESP32 (моно канал DAC1 на GPIO25).
};

/**
 * \brief Конфигурация приёмника аудиопотока от сервера.
 */
struct Config {
  OutputMode mode = OutputMode::InternalDac; ///< Каким способом выводить звук (по умолчанию — встроенный ЦАП на GPIO25).
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
  uint32_t sequence = 0;       ///< Порядковый номер кадра, назначенный сервером.
  uint32_t timestampUs = 0;    ///< Метка времени формирования кадра на сервере (микросекунды).
  uint32_t sampleRate = 0;     ///< Частота дискретизации, Гц.
  uint16_t channels = 0;       ///< Количество каналов (1 или 2).
  uint16_t bitsPerSample = 0;  ///< Глубина сэмпла (поддерживается только 16 бит).
  float volume = 1.0f;         ///< Нормированное усиление (1.0 = без изменений).
  std::vector<int16_t> samples;///< Интерливированный PCM16 (L,R,L,R ...).
};

/**
 * \brief Диагностика приёмника аудио.
 */
struct Stats {
  uint32_t framesAccepted = 0;     ///< Сколько кадров успешно поставлено в очередь.
  uint32_t framesRejected = 0;     ///< Сколько кадров отброшено из-за ошибок формата.
  uint32_t framesPlayed = 0;       ///< Сколько кадров полностью выведено на ЦАП.
  uint32_t decodeErrors = 0;       ///< Количество ошибок разбора заголовка.
  uint32_t queueDrops = 0;         ///< Сколько кадров потеряно из-за переполнения очереди.
  uint32_t bufferUnderruns = 0;    ///< Сколько раз поток иссякал до окончания воспроизведения кадра.
  uint32_t silencePrimed = 0;      ///< Сколько раз буфер ЦАП заполнялся «тишиной» для устранения фонового треска.
  uint32_t lastSequence = 0;       ///< Последний принятый номер кадра.
  uint32_t lastSampleRate = 0;     ///< Последняя частота дискретизации, применённая к ЦАП.
  uint32_t queueDepth = 0;         ///< Текущая глубина очереди кадров.
  uint32_t queueHighWatermark = 0; ///< Максимальная глубина очереди со старта.
  float lastVolume = 0.0f;         ///< Фактически применённая громкость последнего кадра.
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
bool handle_server_frame(const uint8_t* payload, size_t length);

/**
 * \brief Декодирует кадр без постановки в очередь (используется в тестах).
 * \param payload Указатель на начало бинарного сообщения от сервера.
 * \param length  Размер сообщения в байтах.
 * \param out     Структура, в которую будут записаны результаты разбора.
 * \param error   Текстовое описание ошибки формата (если функция вернула false).
 * \return true, если кадр успешно разобран.
 */
bool decode_server_frame(const uint8_t* payload, size_t length, Frame& out, std::string& error);

namespace detail {

/**
 * \brief Конвертирует интерливированный PCM16 в стереопару DAC-слов ESP32.
 *
 * Функция вынесена в заголовок в inline-формате, чтобы модульные тесты на
 * хосте могли гарантированно использовать ту же математику, что и прошивка.
 * Возвращает готовый буфер с чередованием L/R (левый канал заполняется
 * конвертированным значением, правый удерживается в центре диапазона).
 * Дополнительно при необходимости заполняет диагностические параметры.
 */
inline std::vector<uint16_t> convert_pcm_to_dac_words(const Frame& frame,
                                                      float defaultVolume,
                                                      int32_t* outMin = nullptr,
                                                      int32_t* outMax = nullptr,
                                                      uint32_t* clipped = nullptr,
                                                      float* appliedVolume = nullptr) {
  // Реализация копирует проверенный эталон, который разработчик приводил
  // в виде минимального Arduino-скетча: исходные PCM16 переводятся в
  // «смещённый» 8-битный диапазон 0..255 простым сдвигом, после чего каждое
  // значение разворачивается в стереопару. Левый канал (DAC1 = GPIO25)
  // получает фактический уровень, правый (DAC2 = GPIO26) фиксируется в середине
  // диапазона, чтобы не возбуждать шум при незадействованном выводе.
  std::vector<uint16_t> out;
  if (frame.samples.empty()) {
    if (outMin) {
      *outMin = std::numeric_limits<int32_t>::max();
    }
    if (outMax) {
      *outMax = std::numeric_limits<int32_t>::min();
    }
    if (clipped) {
      *clipped = 0;
    }
    if (appliedVolume) {
      *appliedVolume = defaultVolume;
    }
    return out;
  }

  const uint16_t channels = frame.channels == 0 ? 1 : frame.channels;
  const size_t samplesPerChannel = frame.samples.size() / channels;
  if (samplesPerChannel == 0) {
    if (outMin) {
      *outMin = std::numeric_limits<int32_t>::max();
    }
    if (outMax) {
      *outMax = std::numeric_limits<int32_t>::min();
    }
    if (clipped) {
      *clipped = 0;
    }
    if (appliedVolume) {
      *appliedVolume = defaultVolume;
    }
    return out;
  }

  constexpr uint16_t kDacMidWord = 0x8000u;
  out.assign(samplesPerChannel * 2u, kDacMidWord);

  const float requestedVolume = (std::isfinite(frame.volume) && frame.volume > 0.0f)
                                    ? frame.volume
                                    : defaultVolume;
  const float clampedVolume = std::clamp(requestedVolume, 0.0f, 4.0f);
  if (appliedVolume) {
    *appliedVolume = clampedVolume;
  }

  int32_t inputMin = std::numeric_limits<int32_t>::max();
  int32_t inputMax = std::numeric_limits<int32_t>::min();
  uint32_t clippedSamples = 0;

  for (size_t i = 0; i < samplesPerChannel; ++i) {
    int32_t mixed = 0;
    for (uint16_t ch = 0; ch < channels; ++ch) {
      const int32_t value = static_cast<int32_t>(frame.samples[i * channels + ch]);
      inputMin = std::min(inputMin, value);
      inputMax = std::max(inputMax, value);
      mixed += value;
    }

    if (channels > 1) {
      if (mixed >= 0) {
        mixed = (mixed + (channels / 2)) / static_cast<int32_t>(channels);
      } else {
        mixed = (mixed - (channels / 2)) / static_cast<int32_t>(channels);
      }
    }

    const float scaled = static_cast<float>(mixed) * clampedVolume;
    int32_t sample = static_cast<int32_t>(std::lrintf(scaled));
    if (sample < -32768) {
      sample = -32768;
      clippedSamples++;
    } else if (sample > 32767) {
      sample = 32767;
      clippedSamples++;
    }

    const int32_t shifted = sample + 32768; // переводим -32768..32767 в 0..65535
    const uint8_t dacByte = static_cast<uint8_t>(std::clamp<int32_t>(shifted >> 8, 0, 255));
    const uint16_t dacWord = static_cast<uint16_t>(static_cast<uint16_t>(dacByte) << 8);

    out[i * 2u] = dacWord;       // Левый канал (DAC1) получает фактический сэмпл.
    out[i * 2u + 1u] = kDacMidWord; // Правый канал (DAC2) удерживаем в середине.
  }

  if (outMin) {
    *outMin = inputMin;
  }
  if (outMax) {
    *outMax = inputMax;
  }
  if (clipped) {
    *clipped = clippedSamples;
  }

  return out;
}

} // namespace detail

} // namespace AudioPlayback

