#include "audio_playback.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <new>
#include <vector>

#ifdef ARDUINO
#include <Arduino.h>
#include <driver/dac.h>
#include <driver/i2s.h>
#else
#include <mutex>
#endif

namespace AudioPlayback {
namespace {

// --- Константы протокола и аппаратного вывода ---
constexpr uint32_t MIN_IDLE_DELAY_MS = 50;                ///< Минимальная задержка тишины перед автоматическим mute.
#ifdef ARDUINO
constexpr i2s_port_t I2S_PORT = I2S_NUM_0;                ///< Встроенный ЦАП доступен только на I2S0.
constexpr size_t PLAYBACK_TASK_STACK_WORDS = 4096;        ///< Увеличенный стек из-за конвертаций и логов.
constexpr UBaseType_t PLAYBACK_TASK_PRIORITY = 4;         ///< Приоритет выше телеметрии, чтобы не рвалась речь.
#endif

struct PlaybackFrame {
  Frame frame;
};

struct StreamState {
  bool active = false;       ///< Находится ли плеер в режиме приёма аудио от сервера.
  uint32_t sampleRate = 16000; ///< Текущая частота дискретизации активной сессии.
  uint8_t channels = 1;      ///< Количество каналов (сервер шлёт моно, но поле оставляем для совместимости).
  float volume = 1.0f;       ///< Громкость, переданная в audio_start, с падением на значения по умолчанию.
  uint32_t sequence = 0;     ///< Последний назначенный номер чанка.
};

Config gConfig{};
Stats gStats{};
StreamState gStream{};
bool gInitialized = false;
bool gOutputMuted = true;

#ifdef ARDUINO
QueueHandle_t gQueue = nullptr;
TaskHandle_t gTask = nullptr;
portMUX_TYPE gStatsMux = portMUX_INITIALIZER_UNLOCKED;
bool gDriverInstalled = false; ///< Нужен, чтобы аккуратно выключать I2S только если он реально поднят.
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

  Serial.printf("[PLAYBACK] буфер ЦАП заполнен тишиной (%s): %u сэмплов\n",
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
  dac_output_disable(DAC_CHANNEL_1);
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
  dac_output_enable(DAC_CHANNEL_1);
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

bool is_output_muted() { return gOutputMuted; }

void update_queue_depth_metric(size_t depth) {
  lock_stats();
  gStats.queueDepth = static_cast<uint32_t>(depth);
  if (gStats.queueHighWatermark < gStats.queueDepth) {
    gStats.queueHighWatermark = gStats.queueDepth;
  }
  unlock_stats();
}

#ifdef ARDUINO

bool apply_sample_rate(uint32_t sampleRate) {
  if (sampleRate == 0) {
    sampleRate = gConfig.defaultSampleRate;
  }
  if (sampleRate == 0) {
    sampleRate = 16000; // последний рубеж: не позволяем нулевой частоте свалить вывод.
  }

  const esp_err_t rc = i2s_set_clk(I2S_PORT, sampleRate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  if (rc != ESP_OK) {
    Serial.printf("[PLAYBACK] ошибка установки частоты %u Гц: %d\n", static_cast<unsigned>(sampleRate), rc);
    lock_stats();
    gStats.lastError = "i2s-set-clk";
    unlock_stats();
    return false;
  }

  lock_stats();
  gStats.lastSampleRate = sampleRate;
  unlock_stats();
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
        mute_output("таймаут ожидания аудиочанков", true);
      }
      continue;
    }
    if (!holder) {
      update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
      continue;
    }

    const Frame& frame = holder->frame;
    if (!unmute_output("получен чанк")) {
      lock_stats();
      gStats.chunksRejected++;
      gStats.lastError = "unmute-failed";
      unlock_stats();
      Serial.println(F("[PLAYBACK] ошибка: не удалось вывести тракт из mute"));
      delete holder;
      update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
      continue;
    }

    if (!apply_sample_rate(frame.sampleRate)) {
      lock_stats();
      gStats.chunksRejected++;
      unlock_stats();
      delete holder;
      continue;
    }

    std::vector<uint16_t> dacWords = convert_to_dac_words(frame);
    if (dacWords.empty()) {
      lock_stats();
      gStats.bufferUnderruns++;
      gStats.lastError = "empty-frame";
      unlock_stats();
      Serial.printf("[PLAYBACK] предупреждение: чанк #%u пустой, пропущен\n", static_cast<unsigned>(frame.sequence));
      delete holder;
      continue;
    }

    size_t bytesWritten = 0;
    const size_t bytesToWrite = dacWords.size() * sizeof(uint16_t);
    const esp_err_t rc = i2s_write(I2S_PORT,
                                   reinterpret_cast<const char*>(dacWords.data()),
                                   bytesToWrite,
                                   &bytesWritten,
                                   portMAX_DELAY);
    if (rc != ESP_OK || bytesWritten != bytesToWrite) {
      lock_stats();
      gStats.lastError = "i2s-write";
      gStats.chunksRejected++;
      unlock_stats();
      Serial.printf("[PLAYBACK] ошибка вывода чанка #%u: rc=%d bytes=%u/%u\n",
                    static_cast<unsigned>(frame.sequence),
                    rc,
                    static_cast<unsigned>(bytesWritten),
                    static_cast<unsigned>(bytesToWrite));
    } else {
      lock_stats();
      gStats.chunksPlayed++;
      gStats.lastSequence = frame.sequence;
      gStats.lastError.clear();
      unlock_stats();
      Serial.printf("[PLAYBACK] чанк #%u воспроизведён (%u сэмплов, %u Гц, очередь=%u)\n",
                    static_cast<unsigned>(frame.sequence),
                    static_cast<unsigned>(dacWords.size()),
                    static_cast<unsigned>(frame.sampleRate),
                    static_cast<unsigned>(uxQueueMessagesWaiting(gQueue)));
    }

    delete holder;
    update_queue_depth_metric(uxQueueMessagesWaiting(gQueue));
  }
}

#endif // ARDUINO

bool ensure_queue_created() {
#ifdef ARDUINO
  if (!gQueue) {
    gQueue = xQueueCreate(static_cast<UBaseType_t>(std::max<size_t>(gConfig.queueCapacity, 2)), sizeof(PlaybackFrame*));
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

void clear_queue() {
#ifdef ARDUINO
  if (!gQueue) {
    return;
  }
  PlaybackFrame* stale = nullptr;
  while (xQueueReceive(gQueue, &stale, 0) == pdPASS) {
    delete stale;
  }
  xQueueReset(gQueue);
  update_queue_depth_metric(0);
#else
  gPendingFrames.clear();
  update_queue_depth_metric(0);
#endif
}

Frame make_frame_from_pcm(const uint8_t* payload, size_t length) {
  Frame frame{};
  frame.sequence = ++gStream.sequence;
  frame.sampleRate = gStream.sampleRate;
  frame.channels = gStream.channels == 0 ? 1 : gStream.channels;
  frame.volume = gStream.volume;

  const size_t pcmSamples = length / sizeof(int16_t);
  if (pcmSamples == 0) {
    return frame;
  }

  // Сервер присылает моно-поток, но если channels == 2 — дублируем сэмплы на оба канала.
  frame.samples.reserve(pcmSamples * frame.channels);
  for (size_t i = 0; i < pcmSamples; ++i) {
    int16_t sample = 0;
    std::memcpy(&sample, payload + i * sizeof(int16_t), sizeof(int16_t));
    if (frame.channels == 1) {
      frame.samples.push_back(sample);
    } else {
      frame.samples.push_back(sample);
      frame.samples.push_back(sample);
    }
  }

  return frame;
}

} // namespace

bool init(const Config& cfg) {
  shutdown();
  gConfig = cfg;
  configure_idle_timing_from_config();

#ifdef ARDUINO
  i2s_config_t i2sConfig = {};
  i2sConfig.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_DAC_BUILT_IN);
  i2sConfig.sample_rate = cfg.defaultSampleRate == 0 ? 16000 : cfg.defaultSampleRate;
  i2sConfig.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  i2sConfig.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT; // Используем только левый моно-канал DAC1 (GPIO25).
  i2sConfig.communication_format = I2S_COMM_FORMAT_I2S_MSB;
  i2sConfig.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  i2sConfig.dma_buf_count = 6;
  i2sConfig.dma_buf_len = cfg.frameSamplesHint == 0 ? 256 : cfg.frameSamplesHint;
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

  i2s_set_pin(I2S_PORT, nullptr);
  // Включаем только DAC1 (GPIO25): LM386 работает с моно сигналом, поэтому второй канал не задействуем.
  i2s_set_dac_mode(I2S_DAC_CHANNEL_LEFT_EN);
  dac_output_enable(DAC_CHANNEL_1);

  const esp_err_t startRc = i2s_start(I2S_PORT);
  if (startRc != ESP_OK) {
    Serial.printf("[PLAYBACK] ошибка: не удалось стартовать I2S (%d)\n", startRc);
    lock_stats();
    gStats.initialized = false;
    gStats.lastError = "i2s-start";
    unlock_stats();
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    dac_output_disable(DAC_CHANNEL_1);
    return false;
  }

  if (!ensure_queue_created()) {
    lock_stats();
    gStats.chunksRejected++;
    unlock_stats();
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    dac_output_disable(DAC_CHANNEL_1);
    dac_output_disable(DAC_CHANNEL_2);
    return false;
  }

  Serial.printf("[PLAYBACK] I2S-ЦАП запущен: порт=%d, %u Гц, очередь=%u, канал=DAC1\n",
                static_cast<int>(I2S_PORT),
                static_cast<unsigned>(i2sConfig.sample_rate),
                static_cast<unsigned>(gConfig.queueCapacity));
  if (!prime_dma_with_silence("init")) {
    Serial.println(F("[PLAYBACK] предупреждение: не удалось заполнить DMA тишиной при инициализации"));
  }
#else
  (void)cfg;
  ensure_queue_created();
  prime_dma_with_silence("init-host");
#endif

  gInitialized = true;
  gOutputMuted = false;
  record_mute_state(false, false);
  lock_stats();
  gStats.initialized = true;
  gStats.lastSampleRate = gConfig.defaultSampleRate;
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
    Serial.printf("[PLAYBACK] останавливаем I2S-ЦАП: порт=%d\n", static_cast<int>(I2S_PORT));
    i2s_stop(I2S_PORT);
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
  }
  dac_output_disable(DAC_CHANNEL_1);
  dac_output_disable(DAC_CHANNEL_2);
#else
  gPendingFrames.clear();
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

bool start_stream(uint32_t sampleRate, uint8_t channels, float volume) {
  if (!gInitialized) {
    lock_stats();
    gStats.lastError = "not-initialized";
    unlock_stats();
    return false;
  }

  clear_queue();
  gStream.sequence = 0;
  gStream.sampleRate = sampleRate == 0 ? gConfig.defaultSampleRate : sampleRate;
  gStream.channels = channels == 0 ? 1 : channels;
  gStream.volume = (std::isfinite(volume) && volume > 0.0f) ? volume : gConfig.defaultVolume;
  gStream.active = true;

#ifdef ARDUINO
  Serial.printf("[PLAYBACK] audio_start: %u Гц, каналов=%u, громкость=%.2f\n",
                static_cast<unsigned>(gStream.sampleRate),
                static_cast<unsigned>(gStream.channels),
                gStream.volume);
#endif

  lock_stats();
  gStats.lastSampleRate = gStream.sampleRate;
  gStats.lastVolume = gStream.volume;
  gStats.lastError.clear();
  unlock_stats();
  return true;
}

bool feed_stream_chunk(const uint8_t* payload, size_t length) {
  if (!gInitialized) {
    lock_stats();
    gStats.chunksRejected++;
    gStats.lastError = "not-initialized";
    unlock_stats();
    return false;
  }
  if (!gStream.active) {
    lock_stats();
    gStats.chunksRejected++;
    gStats.lastError = "stream-inactive";
    unlock_stats();
    return false;
  }
  if (!payload || length < sizeof(int16_t)) {
    lock_stats();
    gStats.chunksRejected++;
    gStats.lastError = "empty-payload";
    unlock_stats();
    return false;
  }

  Frame frame = make_frame_from_pcm(payload, length);
  if (frame.samples.empty()) {
    lock_stats();
    gStats.chunksRejected++;
    gStats.lastError = "empty-frame";
    unlock_stats();
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
    gStats.chunksRejected++;
    gStats.lastError = "oom";
    unlock_stats();
    Serial.println(F("[PLAYBACK] ошибка: недостаточно памяти для буфера чанка"));
    return false;
  }
  holder->frame = std::move(frame);
  if (xQueueSend(gQueue, &holder, 0) != pdPASS) {
    delete holder;
    lock_stats();
    gStats.queueDrops++;
    gStats.lastError = "queue-full";
    unlock_stats();
    Serial.println(F("[PLAYBACK] очередь переполнена, чанк отброшен"));
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
    gStats.chunksAccepted++;
    gStats.lastSequence = gStream.sequence;
    gStats.lastSampleRate = gStream.sampleRate;
    gStats.lastVolume = gStream.volume;
    gStats.lastError.clear();
    unlock_stats();
#ifdef ARDUINO
    Serial.printf("[PLAYBACK] принят чанк #%u (%u байт, %u Гц, очередь=%u)\n",
                  static_cast<unsigned>(gStream.sequence),
                  static_cast<unsigned>(length),
                  static_cast<unsigned>(gStream.sampleRate),
                  static_cast<unsigned>(uxQueueMessagesWaiting(gQueue)));
#endif
  }

  return accepted;
}

void stop_stream(const char* reason) {
  gStream.active = false;
  clear_queue();
#ifdef ARDUINO
  mute_output(reason ? reason : "audio_end", false);
  Serial.printf("[PLAYBACK] audio_end: очередь очищена, причина=%s\n", reason ? reason : "нет");
#else
  (void)reason;
#endif
}

} // namespace AudioPlayback

