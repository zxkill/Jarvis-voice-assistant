#include "audio_capture.h"

#include <algorithm>
#include <cmath>
#include <string>

#ifdef ARDUINO
#include <Arduino.h>
#include <driver/i2s.h>
#include <esp_timer.h>
#endif

namespace Audio {
namespace {

Config gConfig{};
Diagnostics gDiagnostics{};
PcmChunk gPendingChunk{};
bool gChunkReady = false;
bool gInitialized = false;
bool gCapturePaused = false;        ///< Признак, что поток временно приостановлен по команде сервера.
std::string gPauseReason;           ///< Последняя текстовая причина паузы (для логов).

std::vector<int32_t> gRawBuffer;   ///< Буфер для чтения 32-битных сэмплов I2S.
std::vector<int16_t> gWorkBuffer;  ///< Рабочий буфер с приведёнными значениями.
std::vector<int16_t> gLeftChannel; ///< Копия левого канала для анализа.
std::vector<int16_t> gRightChannel;///< Копия правого канала для анализа.

#ifdef ARDUINO
constexpr i2s_port_t I2S_PORT = I2S_NUM_1; ///< Переключаем микрофоны на I2S1, чтобы освободить I2S0 под ЦАП.
unsigned long gLastLogMs = 0;              ///< Таймер для ограниченного логирования.
#endif

} // namespace

bool init(const Config& cfg) {
  gConfig = cfg;
  gDiagnostics = Diagnostics{};
  gPendingChunk = PcmChunk{};
  gChunkReady = false;
  gInitialized = true;

  gDiagnostics.sampleRate = cfg.sampleRate;
  gDiagnostics.frameSamples = static_cast<uint32_t>(cfg.frameSamples);
  gDiagnostics.localizationEnabled = cfg.enableLocalization;
  gDiagnostics.microphoneSpacingMeters = cfg.microphoneSpacingMeters;

  const size_t totalSamples = cfg.frameSamples * 2; // два канала
  gRawBuffer.assign(totalSamples, 0);
  gWorkBuffer.assign(totalSamples, 0);
  if (cfg.enableLocalization) {
    gLeftChannel.assign(cfg.frameSamples, 0);
    gRightChannel.assign(cfg.frameSamples, 0);
  } else {
    gLeftChannel.clear();
    gRightChannel.clear();
  }

#ifdef ARDUINO
  i2s_config_t i2sConfig = {};
  i2sConfig.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX);
  i2sConfig.sample_rate = cfg.sampleRate;
  i2sConfig.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  i2sConfig.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  i2sConfig.communication_format = I2S_COMM_FORMAT_I2S_MSB;
  i2sConfig.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  i2sConfig.dma_buf_count = 4;
  i2sConfig.dma_buf_len = cfg.frameSamples;
  i2sConfig.use_apll = true;
  i2sConfig.tx_desc_auto_clear = false;
  i2sConfig.fixed_mclk = 0;

  if (i2s_driver_install(I2S_PORT, &i2sConfig, 0, nullptr) != ESP_OK) {
    gInitialized = false;
#ifdef ARDUINO
    Serial.println(F("[AUDIO] ошибка: не удалось установить драйвер I2S"));
#endif
    return false;
  }

  i2s_pin_config_t pinConfig = {};
  pinConfig.bck_io_num = cfg.pinBclk;
  pinConfig.ws_io_num = cfg.pinWs;
  pinConfig.data_out_num = I2S_PIN_NO_CHANGE;
  pinConfig.data_in_num = cfg.pinData;

  if (i2s_set_pin(I2S_PORT, &pinConfig) != ESP_OK) {
    i2s_driver_uninstall(I2S_PORT);
    gInitialized = false;
    Serial.println(F("[AUDIO] ошибка: не удалось назначить пины I2S"));
    return false;
  }

  i2s_set_clk(I2S_PORT, cfg.sampleRate, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_STEREO);

  i2s_zero_dma_buffer(I2S_PORT);
  Serial.printf("[AUDIO] I2S запущен: порт=%d, BCLK=%d WS=%d DATA=%d rate=%u frame=%u\n",
                static_cast<int>(I2S_PORT),
                cfg.pinBclk,
                cfg.pinWs,
                cfg.pinData,
                cfg.sampleRate,
                static_cast<unsigned>(cfg.frameSamples));
  if (cfg.enableLocalization) {
    Serial.println(F("[AUDIO] локализация включена: угол вычисляется на борту"));
  } else {
    Serial.println(F("[AUDIO] локализация выключена: направление определяет сервер"));
  }
#endif

  return true;
}

void shutdown() {
#ifdef ARDUINO
  if (gInitialized) {
    i2s_driver_uninstall(I2S_PORT);
    Serial.println(F("[AUDIO] драйвер I2S остановлен"));
  }
#endif
  gInitialized = false;
  gChunkReady = false;
}

void poll() {
  if (!gInitialized) {
    return;
  }

  if (gCapturePaused) {
    gDiagnostics.streamPaused = true;
    gDiagnostics.streamHasChunk = false;
    return;
  }

#ifdef ARDUINO
  size_t bytesRead = 0;
  const size_t bytesToRead = gRawBuffer.size() * sizeof(int32_t);
  const esp_err_t res = i2s_read(I2S_PORT, gRawBuffer.data(), bytesToRead, &bytesRead, 0);
  if (res != ESP_OK || bytesRead == 0) {
    return;
  }

  const size_t samplesRead = bytesRead / sizeof(int32_t);
  if (samplesRead < 2) {
    return;
  }

  const size_t framesRead = samplesRead / 2;
  if (framesRead == 0) {
    return;
  }

  if (gWorkBuffer.size() < samplesRead) {
    gWorkBuffer.resize(samplesRead);
  }
  for (size_t i = 0; i < samplesRead; ++i) {
    gWorkBuffer[i] = static_cast<int16_t>(gRawBuffer[i] >> 11); // упрощённое преобразование 24->16 бит
  }

  float sumSqLeft = 0.0f;
  float sumSqRight = 0.0f;
  if (gConfig.enableLocalization) {
    if (gLeftChannel.size() < framesRead) {
      gLeftChannel.resize(framesRead);
    }
    if (gRightChannel.size() < framesRead) {
      gRightChannel.resize(framesRead);
    }
  }

  for (size_t i = 0, frame = 0; frame < framesRead; ++frame, i += 2) {
    const int16_t left = gWorkBuffer[i];
    const int16_t right = gWorkBuffer[i + 1];
    sumSqLeft += static_cast<float>(left) * static_cast<float>(left);
    sumSqRight += static_cast<float>(right) * static_cast<float>(right);
    if (gConfig.enableLocalization) {
      gLeftChannel[frame] = left;
      gRightChannel[frame] = right;
    }
  }

  if (framesRead > 0) {
    constexpr float INV_MAX_I16 = 1.0f / 32768.0f;
    const float invSamples = 1.0f / static_cast<float>(framesRead);
    gDiagnostics.rmsLeft = std::sqrt(sumSqLeft * invSamples) * INV_MAX_I16;
    gDiagnostics.rmsRight = std::sqrt(sumSqRight * invSamples) * INV_MAX_I16;
  } else {
    gDiagnostics.rmsLeft = 0.0f;
    gDiagnostics.rmsRight = 0.0f;
  }

  DirectionEstimate lastEstimate{};
  bool haveEstimate = false;
  if (gConfig.enableLocalization && !gLeftChannel.empty() && !gRightChannel.empty()) {
    lastEstimate = estimate_direction(gLeftChannel, gRightChannel,
                                      gConfig.sampleRate, gConfig.microphoneSpacingMeters);
    gDiagnostics.directionDeg = lastEstimate.angleDeg;
    gDiagnostics.confidence = lastEstimate.confidence;
    gDiagnostics.localizationEnabled = true;
    haveEstimate = true;
  } else {
    gDiagnostics.directionDeg = 0.0f;
    gDiagnostics.confidence = 0.0f;
    gDiagnostics.localizationEnabled = false;
  }
  gDiagnostics.sampleRate = gConfig.sampleRate;
  gDiagnostics.frameSamples = static_cast<uint32_t>(framesRead);
  gDiagnostics.framesCaptured += 1;
  gDiagnostics.lastFrameTimestampUs = esp_timer_get_time();
  gDiagnostics.streamHasChunk = true;
  gDiagnostics.streamPaused = false;
  gDiagnostics.microphoneSpacingMeters = gConfig.microphoneSpacingMeters;

  const auto samplesToCopy = static_cast<std::vector<int16_t>::difference_type>(framesRead * 2);
  gPendingChunk.interleaved.assign(gWorkBuffer.begin(), gWorkBuffer.begin() + samplesToCopy);
  gPendingChunk.sampleRate = gConfig.sampleRate;
  gPendingChunk.channels = 2;
  gPendingChunk.timestampUs = gDiagnostics.lastFrameTimestampUs;
  gChunkReady = true;

  const unsigned long now = millis();
  if (now - gLastLogMs > 2000) {
    if (haveEstimate) {
      Serial.printf("[AUDIO] локально: dir=%.1f° conf=%.2f rmsL=%.3f rmsR=%.3f lag=%d\n",
                    lastEstimate.angleDeg, lastEstimate.confidence,
                    gDiagnostics.rmsLeft, gDiagnostics.rmsRight,
                    lastEstimate.bestLagSamples);
    } else {
      Serial.printf("[AUDIO] сервер: rmsL=%.3f rmsR=%.3f кадр=%u\n",
                    gDiagnostics.rmsLeft,
                    gDiagnostics.rmsRight,
                    static_cast<unsigned>(framesRead));
    }
    gLastLogMs = now;
  }
#else
  (void)gRawBuffer;
  (void)gWorkBuffer;
  (void)gLeftChannel;
  (void)gRightChannel;
#endif
}

Diagnostics latest_diagnostics() {
  Diagnostics copy = gDiagnostics;
  copy.streamHasChunk = gChunkReady;
  copy.streamPaused = gCapturePaused;
  return copy;
}

bool pop_chunk(PcmChunk& out) {
  if (!gChunkReady) {
    return false;
  }

  // Используем перенос владения буфером, чтобы сократить копирование крупных
  // массивов PCM. Это уменьшает задержку между захватом и передачей аудио,
  // что особенно важно при высокой частоте кадров.
  out.interleaved = std::move(gPendingChunk.interleaved);
  out.sampleRate = gPendingChunk.sampleRate;
  out.channels = gPendingChunk.channels;
  out.timestampUs = gPendingChunk.timestampUs;

  const size_t capacityHint = out.interleaved.capacity();
  gPendingChunk.interleaved.clear();
  if (capacityHint > 0) {
    // Сохраняем резерв для следующего кадра, чтобы избежать частых реаллокаций при assign().
    gPendingChunk.interleaved.reserve(capacityHint);
  }

  gChunkReady = false;
  gDiagnostics.streamHasChunk = false;
  return true;
}

void set_paused(bool paused, const char* reason) {
  gCapturePaused = paused;
  gPauseReason = reason ? reason : std::string();
  gDiagnostics.streamPaused = gCapturePaused;
  if (paused) {
    gChunkReady = false;
    gDiagnostics.streamHasChunk = false;
#ifdef ARDUINO
    Serial.printf("[AUDIO] захват приостановлен сервером (%s)\n", gPauseReason.c_str());
#endif
  } else {
#ifdef ARDUINO
    Serial.printf("[AUDIO] захват возобновлён (причина: %s)\n", gPauseReason.c_str());
#endif
    gPauseReason.clear();
  }
}

bool is_paused() { return gCapturePaused; }

} // namespace Audio
