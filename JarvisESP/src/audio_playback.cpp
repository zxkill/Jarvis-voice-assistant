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

#ifdef ARDUINO
constexpr i2s_port_t I2S_PORT = I2S_NUM_0;
constexpr size_t PLAYBACK_TASK_STACK_WORDS = 4096;
constexpr UBaseType_t PLAYBACK_TASK_PRIORITY = 4;
#endif

constexpr uint32_t MIN_IDLE_DELAY_MS = 50;

// Для речи важнее не терять PCM-чанки, чем держать минимальную задержку.
// Сервер может отправить аудио быстрее реального времени, поэтому маленькая
// очередь на ESP32 приводит к выпадению слогов, цифр и коротких SFX.
constexpr size_t MIN_PLAYBACK_QUEUE_CAPACITY = 64;
#ifdef ARDUINO
constexpr TickType_t QUEUE_SEND_WAIT_TICKS = pdMS_TO_TICKS(1000);
#endif

struct PlaybackFrame {
  Frame frame;
};

struct StreamState {
  bool active = false;      // Между audio_start и audio_end принимаем новые чанки.
  bool finishing = false;   // audio_end уже пришёл, но очередь нужно доиграть.
  uint32_t sampleRate = 16000;
  uint8_t channels = 1;
  float volume = 1.0f;
  uint32_t sequence = 0;
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
portMUX_TYPE gStreamMux = portMUX_INITIALIZER_UNLOCKED;
bool gDriverInstalled = false;
bool gIdleMuteEnabled = true;
TickType_t gIdleTimeoutTicks = pdMS_TO_TICKS(250);
TickType_t gQueuePollTicks = pdMS_TO_TICKS(50);
uint32_t gAppliedSampleRate = 0; // Реальная частота, уже выставленная в I2S. Не сбрасываем её на каждом PCM-чанке.
#else
std::mutex gStatsMutex;
std::vector<Frame> gPendingFrames;
#endif

void lock_stats() {
#ifdef ARDUINO
  taskENTER_CRITICAL(&gStatsMux);
#else
  gStatsMutex.lock();
#endif
}

void unlock_stats() {
#ifdef ARDUINO
  taskEXIT_CRITICAL(&gStatsMux);
#else
  gStatsMutex.unlock();
#endif
}

#ifdef ARDUINO
void lock_stream() { taskENTER_CRITICAL(&gStreamMux); }
void unlock_stream() { taskEXIT_CRITICAL(&gStreamMux); }
#else
void lock_stream() {}
void unlock_stream() {}
#endif

void set_last_error(const char* error) {
  lock_stats();
  gStats.lastError = error ? error : "";
  unlock_stats();
}

void record_mute_state(bool muted, bool countTransition) {
  lock_stats();
  if (countTransition && muted) {
    gStats.idleTransitions++;
  }
  gStats.muted = muted;
  unlock_stats();
}

void update_queue_depth_metric(size_t depth) {
  lock_stats();
  gStats.queueDepth = static_cast<uint32_t>(depth);
  if (gStats.queueHighWatermark < gStats.queueDepth) {
    gStats.queueHighWatermark = gStats.queueDepth;
  }
  unlock_stats();
}

#ifdef ARDUINO

size_t queue_depth() {
  return gQueue ? uxQueueMessagesWaiting(gQueue) : 0;
}

void configure_idle_timing_from_config() {
  if (gConfig.idleMuteDelayMs == 0) {
    gIdleMuteEnabled = false;
    gIdleTimeoutTicks = 0;
    gQueuePollTicks = pdMS_TO_TICKS(50);
    Serial.println(F("[PLAYBACK] автоматический mute отключён"));
    return;
  }
  const uint32_t delayMs = std::max<uint32_t>(gConfig.idleMuteDelayMs, MIN_IDLE_DELAY_MS);
  gIdleMuteEnabled = true;
  gIdleTimeoutTicks = pdMS_TO_TICKS(delayMs);
  gQueuePollTicks = gIdleTimeoutTicks;
}

uint16_t silence_word() {
  return gConfig.mode == OutputMode::InternalDac ? 0x8000u : 0u;
}

bool prime_dma_with_silence(const char* context) {
  if (!gDriverInstalled) {
    return false;
  }
  size_t samples = gConfig.frameSamplesHint == 0 ? 512 : gConfig.frameSamplesHint;
  samples = std::max<size_t>(samples, 256);
  std::vector<uint16_t> silence(samples, silence_word());
  size_t bytesWritten = 0;
  const size_t bytesToWrite = silence.size() * sizeof(uint16_t);
  const esp_err_t rc = i2s_write(I2S_PORT,
                                 reinterpret_cast<const char*>(silence.data()),
                                 bytesToWrite,
                                 &bytesWritten,
                                 portMAX_DELAY);
  if (rc != ESP_OK || bytesWritten != bytesToWrite) {
    Serial.printf("[PLAYBACK] не удалось заполнить DMA тишиной (%s): rc=%d bytes=%u/%u\n",
                  context ? context : "?",
                  rc,
                  static_cast<unsigned>(bytesWritten),
                  static_cast<unsigned>(bytesToWrite));
    set_last_error("silence-prime");
    return false;
  }
  lock_stats();
  gStats.silencePrimed++;
  unlock_stats();
  return true;
}

bool mute_output(const char* reason, bool countTransition) {
  if (!gDriverInstalled || gOutputMuted) {
    return false;
  }
  Serial.printf("[PLAYBACK] mute: %s\n", reason ? reason : "no-reason");

  // Для внешнего MAX98357A не останавливаем I2S-такты между фразами.
  // i2s_stop/i2s_start часто съедает первые миллисекунды следующего слова.
  // Вместо этого очищаем DMA и держим линию в тишине.
  i2s_zero_dma_buffer(I2S_PORT);
  if (gConfig.mode == OutputMode::InternalDac) {
    const esp_err_t rc = i2s_stop(I2S_PORT);
    if (rc != ESP_OK) {
      Serial.printf("[PLAYBACK] предупреждение: i2s_stop=%d\n", rc);
    }
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
  Serial.printf("[PLAYBACK] unmute: %s\n", reason ? reason : "audio");

  // Внешний I2S уже запущен и молчит, поэтому не перезапускаем драйвер.
  // Это уменьшает обрезание начала TTS.
  if (gConfig.mode == OutputMode::InternalDac) {
    const esp_err_t rc = i2s_start(I2S_PORT);
    if (rc != ESP_OK) {
      Serial.printf("[PLAYBACK] ошибка i2s_start=%d\n", rc);
      set_last_error("i2s-start");
      return false;
    }
    dac_output_enable(DAC_CHANNEL_1);
    i2s_zero_dma_buffer(I2S_PORT);
    prime_dma_with_silence("unmute");
  }

  gOutputMuted = false;
  record_mute_state(false, false);
  return true;
}

bool apply_sample_rate(uint32_t sampleRate) {
  if (sampleRate == 0) {
    sampleRate = gConfig.defaultSampleRate ? gConfig.defaultSampleRate : 16000;
  }

  // Важно: i2s_set_clk нельзя вызывать на каждом PCM-фрейме.
  // На MAX98357A это даёт микропровалы, из-за чего "съедаются" буквы и цифры.
  if (gAppliedSampleRate == sampleRate) {
    lock_stats();
    gStats.lastSampleRate = sampleRate;
    unlock_stats();
    return true;
  }

  const esp_err_t rc = i2s_set_clk(I2S_PORT, sampleRate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  if (rc != ESP_OK) {
    Serial.printf("[PLAYBACK] ошибка установки частоты %u Гц: %d\n", static_cast<unsigned>(sampleRate), rc);
    set_last_error("i2s-set-clk");
    return false;
  }
  gAppliedSampleRate = sampleRate;
  lock_stats();
  gStats.lastSampleRate = sampleRate;
  unlock_stats();
  Serial.printf("[PLAYBACK] I2S sample rate установлен: %u Гц\n", static_cast<unsigned>(sampleRate));
  return true;
}

#else

size_t queue_depth() { return gPendingFrames.size(); }
void configure_idle_timing_from_config() {}
bool prime_dma_with_silence(const char*) {
  lock_stats();
  gStats.silencePrimed++;
  unlock_stats();
  return true;
}
bool mute_output(const char*, bool) { gOutputMuted = true; record_mute_state(true, false); return true; }
bool unmute_output(const char*) { gOutputMuted = false; record_mute_state(false, false); return true; }
bool apply_sample_rate(uint32_t sampleRate) {
  lock_stats();
  gStats.lastSampleRate = sampleRate;
  unlock_stats();
  return true;
}

#endif

bool is_output_muted() {
  return gOutputMuted;
}

bool stream_is_finishing() {
  lock_stream();
  const bool finishing = gStream.finishing;
  unlock_stream();
  return finishing;
}

void clear_finishing_if_needed() {
  lock_stream();
  gStream.finishing = false;
  unlock_stream();
}

#ifdef ARDUINO

void clear_queue() {
  if (!gQueue) {
    update_queue_depth_metric(0);
    return;
  }
  PlaybackFrame* stale = nullptr;
  while (xQueueReceive(gQueue, &stale, 0) == pdPASS) {
    delete stale;
  }
  xQueueReset(gQueue);
  update_queue_depth_metric(0);
}

#else

void clear_queue() {
  gPendingFrames.clear();
  update_queue_depth_metric(0);
}

#endif

std::vector<uint16_t> convert_to_dac_words(const Frame& frame) {
  std::vector<uint16_t> out;
  if (frame.samples.empty()) {
    return out;
  }
  const uint16_t channels = frame.channels == 0 ? 1 : frame.channels;
  const size_t samplesPerChannel = frame.samples.size() / channels;
  out.reserve(samplesPerChannel);

  const float volume = (std::isfinite(frame.volume) && frame.volume > 0.0f)
                           ? frame.volume
                           : gConfig.defaultVolume;
  for (size_t i = 0; i < samplesPerChannel; ++i) {
    int32_t mixed = 0;
    for (uint16_t ch = 0; ch < channels; ++ch) {
      mixed += static_cast<int32_t>(frame.samples[i * channels + ch]);
    }
    mixed /= static_cast<int32_t>(channels);
    int32_t sample = static_cast<int32_t>(std::lround(static_cast<float>(mixed) * volume));
    sample = std::max(-32768, std::min(32767, sample));

    if (gConfig.mode == OutputMode::InternalDac) {
      int32_t biased = std::max(0, std::min(65535, sample + 32768));
      out.push_back(static_cast<uint16_t>((biased >> 8) << 8));
    } else {
      out.push_back(static_cast<uint16_t>(static_cast<int16_t>(sample)));
    }
  }
  lock_stats();
  gStats.lastVolume = volume;
  unlock_stats();
  return out;
}

Frame make_frame_from_pcm(const uint8_t* payload, size_t length) {
  Frame frame{};
  lock_stream();
  frame.sequence = ++gStream.sequence;
  frame.sampleRate = gStream.sampleRate;
  frame.channels = gStream.channels == 0 ? 1 : gStream.channels;
  frame.volume = gStream.volume;
  unlock_stream();

  const size_t pcmSamples = length / sizeof(int16_t);
  frame.samples.reserve(pcmSamples * frame.channels);
  for (size_t i = 0; i < pcmSamples; ++i) {
    int16_t sample = 0;
    std::memcpy(&sample, payload + i * sizeof(int16_t), sizeof(int16_t));
    if (frame.channels == 1) {
      frame.samples.push_back(sample);
    } else {
      for (uint16_t ch = 0; ch < frame.channels; ++ch) {
        frame.samples.push_back(sample);
      }
    }
  }
  return frame;
}

#ifdef ARDUINO

void maybe_finish_after_queue_empty(const char* reason) {
  if (queue_depth() == 0 && stream_is_finishing()) {
    clear_finishing_if_needed();
    mute_output(reason ? reason : "audio finished", false);
    Serial.println(F("[PLAYBACK] поток доигран, очередь пуста"));
  }
}

void playback_task(void*) {
  Serial.println(F("[PLAYBACK] задача воспроизведения запущена"));
  for (;;) {
    PlaybackFrame* holder = nullptr;
    const TickType_t waitTicks = gIdleMuteEnabled ? gIdleTimeoutTicks : gQueuePollTicks;
    if (xQueueReceive(gQueue, &holder, waitTicks) != pdPASS) {
      update_queue_depth_metric(queue_depth());
      if (stream_is_finishing()) {
        maybe_finish_after_queue_empty("audio_end");
      } else if (gIdleMuteEnabled) {
        mute_output("idle timeout", true);
      }
      continue;
    }

    if (!holder) {
      update_queue_depth_metric(queue_depth());
      continue;
    }

    const Frame frame = std::move(holder->frame);
    delete holder;
    update_queue_depth_metric(queue_depth());

    if (!unmute_output("pcm chunk")) {
      lock_stats();
      gStats.chunksRejected++;
      unlock_stats();
      continue;
    }
    if (!apply_sample_rate(frame.sampleRate)) {
      lock_stats();
      gStats.chunksRejected++;
      unlock_stats();
      continue;
    }

    std::vector<uint16_t> dacWords = convert_to_dac_words(frame);
    if (dacWords.empty()) {
      lock_stats();
      gStats.bufferUnderruns++;
      gStats.lastError = "empty-frame";
      unlock_stats();
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
      gStats.chunksRejected++;
      gStats.lastError = "i2s-write";
      unlock_stats();
      Serial.printf("[PLAYBACK] ошибка вывода #%u: rc=%d bytes=%u/%u\n",
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
    }

    maybe_finish_after_queue_empty("audio_end");
  }
}

bool ensure_queue_created() {
  if (!gQueue) {
    const size_t capacity = std::max<size_t>(gConfig.queueCapacity, MIN_PLAYBACK_QUEUE_CAPACITY);
    gQueue = xQueueCreate(static_cast<UBaseType_t>(capacity), sizeof(PlaybackFrame*));
    if (!gQueue) {
      set_last_error("queue-create");
      Serial.println(F("[PLAYBACK] не удалось создать очередь"));
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
      set_last_error("task-create");
      Serial.println(F("[PLAYBACK] не удалось создать задачу"));
      return false;
    }
  }
  return true;
}

#else

bool ensure_queue_created() { return true; }

#endif

} // namespace

bool init(const Config& cfg) {
  shutdown();
  gConfig = cfg;
  configure_idle_timing_from_config();

#ifdef ARDUINO
  i2s_config_t i2sConfig = {};
  i2sConfig.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX);
  if (cfg.mode == OutputMode::InternalDac) {
    i2sConfig.mode = static_cast<i2s_mode_t>(i2sConfig.mode | I2S_MODE_DAC_BUILT_IN);
  }
  i2sConfig.sample_rate = cfg.defaultSampleRate == 0 ? 16000 : cfg.defaultSampleRate;
  i2sConfig.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  i2sConfig.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  i2sConfig.communication_format = I2S_COMM_FORMAT_I2S_MSB;
  i2sConfig.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  i2sConfig.dma_buf_count = 6;
  i2sConfig.dma_buf_len = cfg.frameSamplesHint == 0 ? 512 : cfg.frameSamplesHint;
  i2sConfig.use_apll = false;
  i2sConfig.tx_desc_auto_clear = true;
  i2sConfig.fixed_mclk = 0;

  if (i2s_driver_install(I2S_PORT, &i2sConfig, 0, nullptr) != ESP_OK) {
    set_last_error("i2s-install");
    return false;
  }
  gDriverInstalled = true;

  if (cfg.mode == OutputMode::InternalDac) {
    i2s_set_pin(I2S_PORT, nullptr);
    i2s_set_dac_mode(I2S_DAC_CHANNEL_LEFT_EN);
    dac_output_enable(DAC_CHANNEL_1);
  } else {
    i2s_pin_config_t pinConfig = {};
    pinConfig.bck_io_num = cfg.pinBclk;
    pinConfig.ws_io_num = cfg.pinWs;
    pinConfig.data_out_num = cfg.pinData;
    pinConfig.data_in_num = I2S_PIN_NO_CHANGE;
    if (i2s_set_pin(I2S_PORT, &pinConfig) != ESP_OK) {
      set_last_error("pin-assign");
      i2s_driver_uninstall(I2S_PORT);
      gDriverInstalled = false;
      return false;
    }
  }

  if (i2s_start(I2S_PORT) != ESP_OK) {
    set_last_error("i2s-start");
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    return false;
  }
  gAppliedSampleRate = i2sConfig.sample_rate;
  if (!ensure_queue_created()) {
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
    return false;
  }
  prime_dma_with_silence("init");
  Serial.printf("[PLAYBACK] I2S активен: %u Гц, очередь=%u, режим=%s\n",
                static_cast<unsigned>(i2sConfig.sample_rate),
                static_cast<unsigned>(std::max<size_t>(gConfig.queueCapacity, MIN_PLAYBACK_QUEUE_CAPACITY)),
                cfg.mode == OutputMode::InternalDac ? "dac" : "i2s-ext");
#else
  ensure_queue_created();
  prime_dma_with_silence("host-init");
#endif

  gInitialized = true;
  gOutputMuted = false;
  lock_stream();
  gStream = StreamState{};
  gStream.sampleRate = gConfig.defaultSampleRate ? gConfig.defaultSampleRate : 16000;
  gStream.volume = gConfig.defaultVolume;
  unlock_stream();

  lock_stats();
  gStats = Stats{};
  gStats.initialized = true;
  gStats.lastSampleRate = gStream.sampleRate;
  gStats.muted = false;
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
    i2s_stop(I2S_PORT);
    i2s_driver_uninstall(I2S_PORT);
    gDriverInstalled = false;
  }
  gAppliedSampleRate = 0;
  if (gConfig.mode == OutputMode::InternalDac) {
    dac_output_disable(DAC_CHANNEL_1);
    dac_output_disable(DAC_CHANNEL_2);
  }
#else
  gPendingFrames.clear();
#endif
  gOutputMuted = true;
  gInitialized = false;
  lock_stream();
  gStream = StreamState{};
  unlock_stream();
  reset_stats();
}

void reset_stats() {
  lock_stats();
  Stats fresh{};
  fresh.initialized = gInitialized;
  fresh.muted = is_output_muted();
  gStats = fresh;
  unlock_stats();
}

Stats stats() {
  lock_stats();
  Stats copy = gStats;
  unlock_stats();
#ifdef ARDUINO
  copy.queueDepth = static_cast<uint32_t>(queue_depth());
#else
  copy.queueDepth = static_cast<uint32_t>(gPendingFrames.size());
#endif
  return copy;
}

bool start_stream(uint32_t sampleRate, uint8_t channels, float volume) {
  if (!gInitialized) {
    set_last_error("not-initialized");
    return false;
  }
  // Важно: обычный audio_start больше НЕ чистит очередь.
  // Сервер может начать эффект сразу после TTS, пока последние PCM-фреймы
  // ещё доигрываются на ESP32. Если здесь очистить очередь, у коротких
  // эмоций/сигналов будут пропадать куски, а у речи иногда обрежется хвост.
  // Старые фреймы безопасно доигрываются: каждый Frame хранит свою частоту,
  // громкость и число каналов. Новый поток просто добавляется в очередь далее.
#ifdef ARDUINO
  const size_t pendingBeforeStart = queue_depth();
  if (pendingBeforeStart > 0) {
    Serial.printf("[PLAYBACK] audio_start: в очереди уже %u фреймов, не очищаем\n",
                  static_cast<unsigned>(pendingBeforeStart));
  }
#endif
  const uint32_t streamSampleRate = sampleRate == 0
                                      ? (gConfig.defaultSampleRate ? gConfig.defaultSampleRate : 16000)
                                      : sampleRate;
  const uint8_t streamChannels = channels == 0 ? 1 : channels;
  const float streamVolume = (std::isfinite(volume) && volume > 0.0f)
                                 ? volume
                                 : gConfig.defaultVolume;

#ifdef ARDUINO
  // Выставляем частоту один раз на старте потока, а не на каждом чанке.
  // Это убирает микропаузу в начале слов и на границах фреймов.
  if (!apply_sample_rate(streamSampleRate)) {
    return false;
  }
#endif

  lock_stream();
  gStream.sequence = 0;
  gStream.sampleRate = streamSampleRate;
  gStream.channels = streamChannels;
  gStream.volume = streamVolume;
  gStream.active = true;
  gStream.finishing = false;
  unlock_stream();

  lock_stats();
  gStats.lastSampleRate = streamSampleRate;
  gStats.lastVolume = streamVolume;
  gStats.lastError.clear();
  unlock_stats();
#ifdef ARDUINO
  Serial.printf("[PLAYBACK] audio_start: %u Гц, каналов=%u, громкость=%.2f\n",
                static_cast<unsigned>(streamSampleRate),
                static_cast<unsigned>(streamChannels),
                streamVolume);
#endif
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

  lock_stream();
  const bool canAccept = gStream.active;
  unlock_stream();
  if (!canAccept) {
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
    return false;
  }
  holder->frame = std::move(frame);
  if (xQueueSend(gQueue, &holder, QUEUE_SEND_WAIT_TICKS) != pdPASS) {
    delete holder;
    lock_stats();
    gStats.queueDrops++;
    gStats.chunksRejected++;
    gStats.lastError = "queue-full";
    unlock_stats();
    Serial.println(F("[PLAYBACK] очередь переполнена: PCM-чанк отброшен"));
    return false;
  }
  update_queue_depth_metric(queue_depth());
#else
  gPendingFrames.push_back(std::move(frame));
  update_queue_depth_metric(gPendingFrames.size());
#endif

  lock_stats();
  gStats.chunksAccepted++;
  gStats.lastError.clear();
  unlock_stats();
  return true;
}

void stop_stream(const char* reason) {
  // Важно: audio_end означает «сервер закончил отправлять данные», а не
  // «стереть буфер». Очередь доигрывается, иначе обрезается хвост фразы.
  lock_stream();
  gStream.active = false;
  gStream.finishing = true;
  unlock_stream();
#ifdef ARDUINO
  Serial.printf("[PLAYBACK] audio_end: доигрываем очередь, причина=%s\n", reason ? reason : "нет");
  maybe_finish_after_queue_empty(reason ? reason : "audio_end");
#else
  (void)reason;
#endif
}

} // namespace AudioPlayback
