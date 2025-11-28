#include "audio_playback.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <new>

#ifdef ARDUINO
#include <Arduino.h>
#include <driver/dac.h>
#include <driver/i2s.h>
#else
#include <mutex>
#endif

namespace AudioPlayback {
namespace {

constexpr char MAGIC[2] = {'A', 'P'}; ///< Сигнатура воспроизводимого кадра.
constexpr uint8_t PROTOCOL_VERSION = 1; ///< Поддерживаемая версия протокола.
constexpr size_t HEADER_SIZE = 36;      ///< Размер заголовка в байтах.
constexpr uint32_t MIN_IDLE_DELAY_MS = 50; ///< Минимальная задержка, после которой включаем режим тишины.
#ifdef ARDUINO
constexpr i2s_port_t I2S_PORT = I2S_NUM_0;               ///< Встроенный ЦАП ESP32 доступен только на I2S0, поэтому закрепляем порт.
constexpr size_t PLAYBACK_TASK_STACK_WORDS = 4096;       ///< Увеличенный стек из-за конвертаций и логов.
constexpr UBaseType_t PLAYBACK_TASK_PRIORITY = 4;        ///< Приоритет выше телеметрии, чтобы не рвалась речь.
#endif

#pragma pack(push, 1)
struct PlaybackHeader {
  char magic[2];
  uint8_t version;
  uint8_t flags;
  uint32_t sequence;
  uint32_t timestampUs;
  uint32_t sampleRate;
  uint16_t channels;
  uint16_t bitsPerSample;
  uint32_t frameSamples;
  uint32_t pcmBytes;
  float volume;
  float reserved;
};
#pragma pack(pop)

static_assert(sizeof(PlaybackHeader) == HEADER_SIZE, "Playback header size mismatch");

struct PlaybackFrame {
  Frame frame;
};

Config gConfig{};
Stats gStats{};
bool gInitialized = false;
bool gOutputMuted = true; ///< Состояние тракта воспроизведения актуально и для тестовой сборки.

#ifdef ARDUINO
QueueHandle_t gQueue = nullptr;
TaskHandle_t gTask = nullptr;
portMUX_TYPE gStatsMux = portMUX_INITIALIZER_UNLOCKED;
bool gDriverInstalled = false; ///< Флаг, позволяющий аккуратно выключать I2S только если драйвер реально поднят.
bool gIdleMuteEnabled = true;  ///< Признак, что перевод в тишину по таймауту включён конфигурацией.
TickType_t gIdleTimeoutTicks = 0; ///< Сколько тиков ожидать до гашения тракта.
TickType_t gQueuePollTicks = 0;   ///< Период опроса очереди, когда таймаут отключён.
#else
std::mutex gStatsMutex;
std::vector<Frame> gPendingFrames;
#endif

inline void lock_stats() {
#ifdef ARDUINO
  taskENTER_CRITICAL(&gStatsMux);
#else
  gStatsMutex.lock();
#endif
}

inline void unlock_stats() {
#ifdef ARDUINO
  taskEXIT_CRITICAL(&gStatsMux);
#else
  gStatsMutex.unlock();
#endif
}

void record_mute_state(bool muted, bool countTransition) {
  lock_stats();
  if (countTransition && muted) {
    gStats.idleTransitions++;
  }
  gStats.muted = muted;
  unlock_stats();
}

#ifdef ARDUINO

/**
 * \brief Проверяет, активирован ли режим встроенного ЦАП, чтобы отличить его от внешнего усилителя MAX98357A.
 */
bool is_dac_mode() {
  return gConfig.mode == OutputMode::InternalDac;
}

void configure_idle_timing_from_config() {
  if (gConfig.idleMuteDelayMs == 0) {
    // Пользователь отключил автоматический mute: оставляем лишь периодический опрос очереди,
    // чтобы задача могла обработать сигнал остановки.
    gIdleMuteEnabled = false;
    gIdleTimeoutTicks = 0;
    gQueuePollTicks = pdMS_TO_TICKS(100);
    Serial.println(F("[PLAYBACK] автоматический mute отключён конфигурацией"));
    return;
  }

  const uint32_t clamped = std::max<uint32_t>(gConfig.idleMuteDelayMs, MIN_IDLE_DELAY_MS);
  gIdleMuteEnabled = true;
  gIdleTimeoutTicks = pdMS_TO_TICKS(clamped);
  gQueuePollTicks = gIdleTimeoutTicks;
  Serial.printf("[PLAYBACK] mute по таймауту активен: %u мс ожидания\n", static_cast<unsigned>(clamped));
}

/**
 * \brief Настраивает вывод аудио в зависимости от выбранного режима.
 *
 * Для MAX98357A проверяем заполненность пинов и явно отключаем встроенный ЦАП,
 * чтобы исключить фоновые наводки на GPIO25/26. Для DAC-режима дополнительно
 * включаем только левый канал, так как LM386 ожидает моно-сигнал.
 */
bool configure_i2s_pins_for_mode() {
  if (is_dac_mode()) {
    i2s_set_pin(I2S_PORT, nullptr);
    i2s_set_dac_mode(I2S_DAC_CHANNEL_LEFT_EN);
    dac_output_enable(DAC_CHANNEL_1);
    dac_output_disable(DAC_CHANNEL_2);
    Serial.println(F("[PLAYBACK] выбран режим встроенного ЦАП (DAC1 на GPIO25)"));
    return true;
  }

  if (gConfig.pinBclk < 0 || gConfig.pinLrc < 0 || gConfig.pinDin < 0) {
    Serial.println(F("[PLAYBACK] ошибка конфигурации: пины BCLK/LRC/DIN должны быть заданы для MAX98357A"));
    lock_stats();
    gStats.lastError = "bad-pins";
    unlock_stats();
    return false;
  }

  i2s_pin_config_t pinConfig = {};
  pinConfig.mck_io_num = I2S_PIN_NO_CHANGE;
  pinConfig.bck_io_num = gConfig.pinBclk;
  pinConfig.ws_io_num = gConfig.pinLrc;
  pinConfig.data_out_num = gConfig.pinDin;
  pinConfig.data_in_num = I2S_PIN_NO_CHANGE;

  const esp_err_t pinRc = i2s_set_pin(I2S_PORT, &pinConfig);
  if (pinRc != ESP_OK) {
    Serial.printf("[PLAYBACK] ошибка назначения пинов для MAX98357A: rc=%d\n", pinRc);
    lock_stats();
    gStats.lastError = "pin-config";
    unlock_stats();
    return false;
  }

  dac_output_disable(DAC_CHANNEL_1);
  dac_output_disable(DAC_CHANNEL_2);
  Serial.printf("[PLAYBACK] выбран режим MAX98357A: BCLK=%d, LRC=%d, DIN=%d\n",
                gConfig.pinBclk,
                gConfig.pinLrc,
                gConfig.pinDin);
  return true;
}

bool prime_dma_with_silence(const char* context) {
  size_t samples = gConfig.frameSamplesHint == 0 ? 512 : gConfig.frameSamplesHint;
  samples = std::max<size_t>(samples, 256);

  std::vector<uint16_t> silence(samples, 0x8000u); // 0x8000 = половина диапазона, что соответствует 0 В.
  size_t bytesWritten = 0;
  const size_t bytesToWrite = silence.size() * sizeof(uint16_t);
  const esp_err_t primeRc =
      i2s_write(I2S_PORT, reinterpret_cast<const char*>(silence.data()), bytesToWrite, &bytesWritten, portMAX_DELAY);
  if (primeRc != ESP_OK || bytesWritten != bytesToWrite) {
    Serial.printf("[PLAYBACK] ошибка заполнения DMA тишиной (%s): rc=%d bytes=%u/%u\n",
                  context ? context : "context?",
                  primeRc,
                  static_cast<unsigned>(bytesWritten),
                  static_cast<unsigned>(bytesToWrite));
    lock_stats();
    gStats.lastError = "silence-prime";
    unlock_stats();
    return false;
  }

  Serial.printf("[PLAYBACK] буфер I2S заполнен тишиной (%s): %u сэмплов\n",
                context ? context : "no-context",
                static_cast<unsigned>(samples));
  lock_stats();
  gStats.silencePrimed++;
  unlock_stats();
  return true;
}

bool mute_output(const char* reason, bool countTransition) {
  if (!gDriverInstalled || gOutputMuted) {
    return false;
  }

  Serial.printf("[PLAYBACK] тракт переведён в тишину: %s\n", reason ? reason : "(нет описания)");
  i2s_zero_dma_buffer(I2S_PORT);
  const esp_err_t stopRc = i2s_stop(I2S_PORT);
  if (stopRc != ESP_OK) {
    Serial.printf("[PLAYBACK] предупреждение: i2s_stop вернул %d\n", stopRc);
  }
  if (is_dac_mode()) {
    dac_output_disable(DAC_CHANNEL_1);
  }
  gOutputMuted = true;
  record_mute_state(true, countTransition);
  return true;
}

bool unmute_output(const char* reason) {
  if (!gDriverInstalled) {
    return false;
  }
  if (!gOutputMuted) {
    return true;
  }

  Serial.printf("[PLAYBACK] тракт пробуждён для воспроизведения: %s\n", reason ? reason : "(нет описания)");
  const esp_err_t startRc = i2s_start(I2S_PORT);
  if (startRc != ESP_OK) {
    Serial.printf("[PLAYBACK] критическая ошибка: i2s_start=%d\n", startRc);
    lock_stats();
    gStats.lastError = "i2s-start";
    unlock_stats();
    return false;
  }
  if (is_dac_mode()) {
    dac_output_enable(DAC_CHANNEL_1);
  }
  i2s_zero_dma_buffer(I2S_PORT);
  if (!prime_dma_with_silence("пробуждение")) {
    Serial.println(F("[PLAYBACK] ошибка: не удалось подать тишину при выходе из mute"));
    return false;
  }
  gOutputMuted = false;
  record_mute_state(false, false);
  return true;
}

#else

void configure_idle_timing_from_config() {}

bool prime_dma_with_silence(const char*) {
  lock_stats();
  gStats.silencePrimed++;
  unlock_stats();
  return true;
}

bool mute_output(const char*, bool) { return true; }

bool unmute_output(const char*) { return true; }

#endif

bool is_output_muted() {
  return gOutputMuted;
}

void update_queue_depth_metric(size_t depth) {
  lock_stats();
  gStats.queueDepth = static_cast<uint32_t>(depth);
  if (gStats.queueHighWatermark < gStats.queueDepth) {
    gStats.queueHighWatermark = gStats.queueDepth;
  }
  unlock_stats();
}


std::vector<int16_t> build_i2s_stereo_frame(const Frame& frame) {
  std::vector<int16_t> out;
  if (frame.samples.empty()) {
    return out;
  }

  const uint16_t channels = frame.channels == 0 ? 1 : frame.channels;
  const size_t samplesPerChannel = frame.samples.size() / channels;
  out.resize(samplesPerChannel * 2); // всегда готовим L+R.

  const float requestedVolume = (std::isfinite(frame.volume) && frame.volume > 0.0f)
                                    ? frame.volume
                                    : gConfig.defaultVolume;

  auto clamp16 = [](int32_t value) {
    if (value < -32768) {
      return static_cast<int16_t>(-32768);
    }
    if (value > 32767) {
      return static_cast<int16_t>(32767);
    }
    return static_cast<int16_t>(value);
  };

  for (size_t i = 0; i < samplesPerChannel; ++i) {
    const size_t base = i * channels;
    int32_t left = static_cast<int32_t>(frame.samples[base]);
    int32_t right = (channels > 1) ? static_cast<int32_t>(frame.samples[base + 1]) : left;

    if (channels > 2) {
      for (uint16_t ch = 2; ch < channels; ++ch) {
        const int32_t sample = static_cast<int32_t>(frame.samples[base + ch]);
        left += sample;
        right += sample;
      }
      left /= static_cast<int32_t>(channels);
      right /= static_cast<int32_t>(channels);
    }

    const int32_t leftScaled = static_cast<int32_t>(std::lround(static_cast<float>(left) * requestedVolume));
    const int32_t rightScaled = static_cast<int32_t>(std::lround(static_cast<float>(right) * requestedVolume));
    out[i * 2] = clamp16(leftScaled);
    out[i * 2 + 1] = clamp16(rightScaled);
  }

  lock_stats();
  gStats.lastVolume = requestedVolume;
  unlock_stats();

  return out;
}

#ifdef ARDUINO

bool apply_sample_rate(uint32_t sampleRate) {
  if (sampleRate == 0) {
    sampleRate = gConfig.defaultSampleRate;
  }
  if (sampleRate == 0) {
    sampleRate = 16000; // последний рубеж: не позволяем нулевой частоте свалить вывод.
  }

  const i2s_channel_t channelMode = is_dac_mode() ? I2S_CHANNEL_MONO : I2S_CHANNEL_STEREO;
  const esp_err_t rc = i2s_set_clk(I2S_PORT, sampleRate, I2S_BITS_PER_SAMPLE_16BIT, channelMode);
  if (rc != ESP_OK) {
    Serial.printf("[PLAYBACK] ошибка установки частоты %u Гц (каналы %s): %d\n",
                  static_cast<unsigned>(sampleRate),
                  channelMode == I2S_CHANNEL_MONO ? "моно" : "стерео",
                  rc);
    lock_stats();
    gStats.lastError = "i2s-set-clk";
    unlock_stats();
    return false;
  }

  lock_stats();
  gStats.lastSampleRate = sampleRate;
  unlock_stats();
  Serial.printf("[PLAYBACK] частота I2S установлена на %u Гц, режим %s\n",
                static_cast<unsigned>(sampleRate),
                channelMode == I2S_CHANNEL_MONO ? "моно" : "стерео");
  return true;
}

std::vector<uint16_t> convert_to_dac_words(const Frame& frame) {
  std::vector<uint16_t> out;
  if (frame.samples.empty()) {
    return out;
  }

  const uint16_t channels = frame.channels == 0 ? 1 : frame.channels;
  const size_t samplesPerChannel = frame.samples.size() / channels;
  out.resize(samplesPerChannel);

  const float requestedVolume = (std::isfinite(frame.volume) && frame.volume > 0.0f)
                                    ? frame.volume
                                    : gConfig.defaultVolume;

  for (size_t i = 0; i < samplesPerChannel; ++i) {
    int32_t mixed = 0;
    for (uint16_t ch = 0; ch < channels; ++ch) {
      mixed += static_cast<int32_t>(frame.samples[i * channels + ch]);
    }
    mixed /= static_cast<int32_t>(channels);

    const float scaled = static_cast<float>(mixed) * requestedVolume;
    int32_t sample = static_cast<int32_t>(std::lround(scaled));
    sample = std::max(-32768, std::min(32767, sample));
    int32_t biased = sample + 32768;
    if (biased < 0) {
      biased = 0;
    }
    if (biased > 65535) {
      biased = 65535;
    }
    const uint16_t dacValue = static_cast<uint16_t>((biased >> 8) << 8);
    out[i] = dacValue;
  }

  lock_stats();
  gStats.lastVolume = requestedVolume;
  unlock_stats();

  return out;
}

void playback_task(void*) {
  Serial.println(F("[PLAYBACK] задача воспроизведения запущена"));
  for (;;) {
    PlaybackFrame* holder = nullptr;
    const TickType_t waitTicks = gIdleMuteEnabled ? gIdleTimeoutTicks : gQueuePollTicks;
    if (xQueueReceive(gQueue, &holder, waitTicks) != pdPASS) {
      update_queue_depth_metric(0);
      if (gIdleMuteEnabled) {
        mute_output("таймаут ожидания аудиокадров", true);
      }
      continue;
    }
    if (!holder) {
      update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
      continue;
    }

    const Frame& frame = holder->frame;
    if (!unmute_output("получен кадр")) {
      lock_stats();
      gStats.framesRejected++;
      gStats.lastError = "unmute-failed";
      unlock_stats();
      Serial.println(F("[PLAYBACK] ошибка: не удалось вывести тракт из mute"));
      delete holder;
      update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
      continue;
    }

    const uint32_t sampleRate = frame.sampleRate ? frame.sampleRate : gConfig.defaultSampleRate;
    if (!apply_sample_rate(sampleRate)) {
      lock_stats();
      gStats.framesRejected++;
      unlock_stats();
      delete holder;
      continue;
    }

    const bool dacMode = is_dac_mode();
    size_t bytesWritten = 0;

    if (dacMode) {
      std::vector<uint16_t> dacWords = convert_to_dac_words(frame);
      if (dacWords.empty()) {
        lock_stats();
        gStats.bufferUnderruns++;
        gStats.lastError = "empty-frame";
        unlock_stats();
        Serial.printf("[PLAYBACK] предупреждение: кадр #%u пустой (DAC), пропущен\n",
                      static_cast<unsigned>(frame.sequence));
        delete holder;
        continue;
      }

      const size_t bytesToWrite = dacWords.size() * sizeof(uint16_t);
      const esp_err_t rc = i2s_write(I2S_PORT,
                                     reinterpret_cast<const char*>(dacWords.data()),
                                     bytesToWrite,
                                     &bytesWritten,
                                     portMAX_DELAY);
      if (rc != ESP_OK || bytesWritten != bytesToWrite) {
        lock_stats();
        gStats.lastError = "i2s-write";
        gStats.framesRejected++;
        unlock_stats();
        Serial.printf("[PLAYBACK] ошибка вывода кадра #%u (DAC): rc=%d bytes=%u/%u\n",
                      static_cast<unsigned>(frame.sequence),
                      rc,
                      static_cast<unsigned>(bytesWritten),
                      static_cast<unsigned>(bytesToWrite));
      } else {
        lock_stats();
        gStats.framesPlayed++;
        gStats.lastSequence = frame.sequence;
        gStats.lastError.clear();
        unlock_stats();
        Serial.printf("[PLAYBACK] кадр #%u воспроизведён (DAC %u сэмплов, %u Гц, очередь=%u)\n",
                      static_cast<unsigned>(frame.sequence),
                      static_cast<unsigned>(dacWords.size()),
                      static_cast<unsigned>(sampleRate),
                      static_cast<unsigned>(uxQueueMessagesWaiting(gQueue)));
      }
    } else {
      std::vector<int16_t> stereo = build_i2s_stereo_frame(frame);
      if (stereo.empty()) {
        lock_stats();
        gStats.bufferUnderruns++;
        gStats.lastError = "empty-frame";
        unlock_stats();
        Serial.printf("[PLAYBACK] предупреждение: кадр #%u пустой (I2S), пропущен\n",
                      static_cast<unsigned>(frame.sequence));
        delete holder;
        continue;
      }

      const size_t bytesToWrite = stereo.size() * sizeof(int16_t);
      const esp_err_t rc = i2s_write(I2S_PORT,
                                     reinterpret_cast<const char*>(stereo.data()),
                                     bytesToWrite,
                                     &bytesWritten,
                                     portMAX_DELAY);
      if (rc != ESP_OK || bytesWritten != bytesToWrite) {
        lock_stats();
        gStats.lastError = "i2s-write";
        gStats.framesRejected++;
        unlock_stats();
        Serial.printf("[PLAYBACK] ошибка вывода кадра #%u (I2S MAX98357A): rc=%d bytes=%u/%u\n",
                      static_cast<unsigned>(frame.sequence),
                      rc,
                      static_cast<unsigned>(bytesWritten),
                      static_cast<unsigned>(bytesToWrite));
      } else {
        lock_stats();
        gStats.framesPlayed++;
        gStats.lastSequence = frame.sequence;
        gStats.lastError.clear();
        unlock_stats();
        Serial.printf("[PLAYBACK] кадр #%u воспроизведён (I2S %u сэмплов L/R, %u Гц, очередь=%u)\n",
                      static_cast<unsigned>(frame.sequence),
                      static_cast<unsigned>(stereo.size() / 2),
                      static_cast<unsigned>(sampleRate),
                      static_cast<unsigned>(uxQueueMessagesWaiting(gQueue)));
      }
    }

    delete holder;
    update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
  }
}

#endif // ARDUINO

bool ensure_queue_created() {
#ifdef ARDUINO
  if (!gQueue) {
    gQueue = xQueueCreate(static_cast<UBaseType_t>(std::max<size_t>(gConfig.queueCapacity, 2)),
                          sizeof(PlaybackFrame*));
    if (!gQueue) {
      lock_stats();
      gStats.lastError = "queue-create";
      unlock_stats();
      Serial.println(F("[PLAYBACK] критическая ошибка: не удалось создать очередь воспроизведения"));
      return false;
    }
  }
  if (!gTask) {
    const BaseType_t rc = xTaskCreatePinnedToCore(playback_task,
                                                  "audio_playback",
                                                  PLAYBACK_TASK_STACK_WORDS,
                                                  nullptr,
                                                  PLAYBACK_TASK_PRIORITY,
                                                  &gTask,
                                                  0);
    if (rc != pdPASS) {
      Serial.println(F("[PLAYBACK] ошибка создания задачи воспроизведения"));
      lock_stats();
      gStats.lastError = "task-create";
      unlock_stats();
      return false;
    }
  }
#endif
  return true;
}

} // namespace

bool decode_server_frame(const uint8_t* payload, size_t length, Frame& out, std::string& error) {
  if (!payload || length < HEADER_SIZE) {
    error = "short-frame";
    return false;
  }

  PlaybackHeader header{};
  std::memcpy(&header, payload, sizeof(header));

  if (std::memcmp(header.magic, MAGIC, sizeof(MAGIC)) != 0) {
    error = "bad-magic";
    return false;
  }
  if (header.version != PROTOCOL_VERSION) {
    error = "bad-version";
    return false;
  }
  if (header.bitsPerSample != 16) {
    error = "bad-bits";
    return false;
  }
  if (header.channels == 0 || header.channels > 2) {
    error = "bad-channels";
    return false;
  }

  const size_t payloadBytes = length - HEADER_SIZE;
  if (payloadBytes != header.pcmBytes) {
    error = "bad-size";
    return false;
  }

  const size_t expectedBytes = static_cast<size_t>(header.frameSamples) * header.channels * sizeof(int16_t);
  if (expectedBytes != header.pcmBytes) {
    error = "bad-frameSamples";
    return false;
  }

  out = Frame{};
  out.sequence = header.sequence;
  out.timestampUs = header.timestampUs;
  out.sampleRate = header.sampleRate;
  out.channels = header.channels;
  out.bitsPerSample = header.bitsPerSample;
  if (std::isfinite(header.volume) && header.volume > 0.0f) {
    out.volume = header.volume;
  } else {
    out.volume = gConfig.defaultVolume;
  }
  out.samples.resize(header.pcmBytes / sizeof(int16_t));
  std::memcpy(out.samples.data(), payload + HEADER_SIZE, header.pcmBytes);
  return true;
}

bool init(const Config& cfg) {
  shutdown();
  gConfig = cfg;
  configure_idle_timing_from_config();

#ifdef ARDUINO
  i2s_config_t i2sConfig = {};
  i2sConfig.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX);
  if (is_dac_mode()) {
    // В режиме встроенного ЦАП добавляем соответствующий флаг, чтобы ESP-IDF подключил DAC1.
    i2sConfig.mode = static_cast<i2s_mode_t>(i2sConfig.mode | I2S_MODE_DAC_BUILT_IN);
  }
  i2sConfig.sample_rate = cfg.defaultSampleRate == 0 ? 16000 : cfg.defaultSampleRate;
  i2sConfig.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  if (is_dac_mode()) {
    // Для DAC оставляем моно и минимальные буферы, чтобы уменьшить задержку отклика.
    i2sConfig.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
    i2sConfig.communication_format = I2S_COMM_FORMAT_I2S_MSB;
    i2sConfig.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    i2sConfig.dma_buf_count = 6;
    i2sConfig.dma_buf_len = cfg.frameSamplesHint == 0 ? 256 : cfg.frameSamplesHint;
  } else {
    // Для MAX98357A включаем полноценное стерео, как в эталонном примере с XiaoZhi.
    i2sConfig.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
    i2sConfig.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    i2sConfig.intr_alloc_flags = 0;
    i2sConfig.dma_buf_count = 8;
    i2sConfig.dma_buf_len = cfg.frameSamplesHint == 0 ? 512 : std::max<size_t>(512, cfg.frameSamplesHint);
  }
  i2sConfig.use_apll = false;
  i2sConfig.tx_desc_auto_clear = true;
  i2sConfig.fixed_mclk = 0;

  if (i2s_driver_install(I2S_PORT, &i2sConfig, 0, nullptr) != ESP_OK) {
    Serial.println(F("[PLAYBACK] ошибка: не удалось установить драйвер I2S"));
    lock_stats();
    gStats.initialized = false;
    gStats.lastError = "i2s-install";
    unlock_stats();
    return false;
  }

  gDriverInstalled = true; // Запоминаем успешный старт, чтобы грамотно гасить порт в shutdown().

  if (!configure_i2s_pins_for_mode()) {
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    return false;
  }

  const esp_err_t startRc = i2s_start(I2S_PORT);
  if (startRc != ESP_OK) {
    Serial.printf("[PLAYBACK] ошибка: не удалось стартовать I2S (%d)\n", startRc);
    lock_stats();
    gStats.initialized = false;
    gStats.lastError = "i2s-start";
    unlock_stats();
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    if (is_dac_mode()) {
      dac_output_disable(DAC_CHANNEL_1);
    }
    return false;
  }

  if (!ensure_queue_created()) {
    lock_stats();
    gStats.framesRejected++;
    unlock_stats();
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    dac_output_disable(DAC_CHANNEL_1);
    dac_output_disable(DAC_CHANNEL_2);
    return false;
  }

  if (is_dac_mode()) {
    Serial.printf("[PLAYBACK] I2S-ЦАП запущен: порт=%d, %u Гц, очередь=%u, канал=DAC1\n",
                  static_cast<int>(I2S_PORT),
                  static_cast<unsigned>(i2sConfig.sample_rate),
                  static_cast<unsigned>(cfg.queueCapacity));
  } else {
    Serial.printf("[PLAYBACK] I2S MAX98357A запущен: порт=%d, %u Гц, очередь=%u, стерео L/R\n",
                  static_cast<int>(I2S_PORT),
                  static_cast<unsigned>(i2sConfig.sample_rate),
                  static_cast<unsigned>(cfg.queueCapacity));
  }
#else
  (void)ensure_queue_created;
#endif

  gOutputMuted = false;
  gInitialized = true;
  reset_stats();
  record_mute_state(false, false);

#ifdef ARDUINO
  if (!prime_dma_with_silence("инициализация")) {
    Serial.println(F("[PLAYBACK] критическая ошибка: не удалось подготовить тишину в DMA"));
    shutdown();
    return false;
  }
#else
  prime_dma_with_silence("init-stub");
#endif

#ifdef ARDUINO
  mute_output("ожидание аудиопотока", true);
#endif

  lock_stats();
  gStats.initialized = true;
  gStats.lastError.clear();
  unlock_stats();
  return true;
}

void shutdown() {
#ifdef ARDUINO
  if (gTask) {
    vTaskDelete(gTask);
    gTask = nullptr;
  }
  if (gQueue) {
    PlaybackFrame* holder = nullptr;
    while (xQueueReceive(gQueue, &holder, 0) == pdPASS) {
      delete holder;
    }
    vQueueDelete(gQueue);
    gQueue = nullptr;
  }
  if (gDriverInstalled) {
    Serial.printf("[PLAYBACK] останавливаем I2S тракт вывода: порт=%d\n", static_cast<int>(I2S_PORT));
    i2s_stop(I2S_PORT);
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
  }
  if (is_dac_mode()) {
    dac_output_disable(DAC_CHANNEL_1);
    // DAC2 не использовался, но отключаем его явно, чтобы исключить паразитную утечку тока при повторных инициализациях.
    dac_output_disable(DAC_CHANNEL_2);
  }
#endif
  gOutputMuted = true;
  record_mute_state(true, false);
  gInitialized = false;
  reset_stats();
}

void reset_stats() {
  lock_stats();
  Stats fresh{};
  fresh.initialized = gInitialized;
  fresh.muted = is_output_muted();
  gStats = fresh;
#ifndef ARDUINO
  gPendingFrames.clear();
#endif
  unlock_stats();
}

Stats stats() {
  lock_stats();
  Stats copy = gStats;
  unlock_stats();
  return copy;
}

bool handle_server_frame(const uint8_t* payload, size_t length) {
  Frame frame;
  std::string error;
  if (!decode_server_frame(payload, length, frame, error)) {
    lock_stats();
    gStats.decodeErrors++;
    gStats.framesRejected++;
    gStats.lastError = error;
    unlock_stats();
#ifdef ARDUINO
    Serial.printf("[PLAYBACK] ошибка разбора кадра: %s\n", error.c_str());
#endif
    return false;
  }

  if (!gInitialized) {
    lock_stats();
    gStats.framesRejected++;
    gStats.lastError = "not-initialized";
    unlock_stats();
#ifdef ARDUINO
    Serial.println(F("[PLAYBACK] предупреждение: кадр получен до инициализации"));
#endif
    return false;
  }

  bool accepted = false;
#ifdef ARDUINO
  if (!ensure_queue_created()) {
    return false;
  }
  PlaybackFrame* holder = new (std::nothrow) PlaybackFrame();
  if (!holder) {
    lock_stats();
    gStats.queueDrops++;
    gStats.framesRejected++;
    gStats.lastError = "oom";
    unlock_stats();
    Serial.println(F("[PLAYBACK] ошибка: недостаточно памяти для буфера кадра"));
    return false;
  }
  holder->frame = std::move(frame);
  if (xQueueSend(gQueue, &holder, 0) != pdPASS) {
    delete holder;
    lock_stats();
    gStats.queueDrops++;
    gStats.lastError = "queue-full";
    unlock_stats();
    Serial.println(F("[PLAYBACK] очередь переполнена, кадр отброшен"));
  } else {
    accepted = true;
    update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
  }
#else
  (void)payload;
  (void)length;
  gPendingFrames.push_back(std::move(frame));
  accepted = true;
  update_queue_depth_metric(gPendingFrames.size());
#endif

  if (accepted) {
    lock_stats();
    gStats.framesAccepted++;
    gStats.lastSequence = frame.sequence;
    gStats.lastSampleRate = frame.sampleRate ? frame.sampleRate : gConfig.defaultSampleRate;
    gStats.lastVolume = frame.volume;
    gStats.lastError.clear();
    unlock_stats();
#ifdef ARDUINO
    Serial.printf("[PLAYBACK] принят кадр #%u (%u Гц, %u каналов, объём %.2f, очередь=%u)\n",
                  static_cast<unsigned>(frame.sequence),
                  static_cast<unsigned>(frame.sampleRate),
                  static_cast<unsigned>(frame.channels),
                  frame.volume,
                  static_cast<unsigned>(uxQueueMessagesWaiting(gQueue)));
#endif
  }

  return accepted;
}

} // namespace AudioPlayback

