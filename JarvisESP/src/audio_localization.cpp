#include "audio_localization.h"

#include <algorithm>
#include <cmath>

namespace Audio {
namespace {

/**
 * \brief Ограничивает значение в диапазоне [-1; 1] без зависимости от Arduino.
 */
float clamp_unit(float value) {
  if (value > 1.0f) return 1.0f;
  if (value < -1.0f) return -1.0f;
  return value;
}

} // namespace

DirectionEstimate estimate_direction(const std::vector<int16_t>& left,
                                     const std::vector<int16_t>& right,
                                     uint32_t sampleRate,
                                     float microphoneSpacingMeters,
                                     float soundSpeed) {
  DirectionEstimate result{};

  const size_t sampleCount = std::min(left.size(), right.size());
  if (sampleCount == 0 || sampleRate == 0 || microphoneSpacingMeters <= 0.0f || soundSpeed <= 0.0f) {
    return result;
  }

  // --- Энергия каналов нужна для нормировки взаимной корреляции ---
  long double energyLeft = 0.0;
  long double energyRight = 0.0;
  for (size_t i = 0; i < sampleCount; ++i) {
    energyLeft  += static_cast<long double>(left[i]) * static_cast<long double>(left[i]);
    energyRight += static_cast<long double>(right[i]) * static_cast<long double>(right[i]);
  }
  if (energyLeft <= 0.0 || energyRight <= 0.0) {
    return result;
  }

  // --- Максимально возможная задержка по геометрии ---
  const float maxTimeDelay = microphoneSpacingMeters / soundSpeed;
  const int maxLagSamples = static_cast<int>(std::ceil(maxTimeDelay * sampleRate));
  if (maxLagSamples <= 0) {
    return result;
  }

  long double bestCorrelation = 0.0;
  int bestLag = 0;

  // --- Поиск максимума корреляции в пределах допустимой задержки ---
  for (int lag = -maxLagSamples; lag <= maxLagSamples; ++lag) {
    long double sum = 0.0;
    for (size_t i = 0; i < sampleCount; ++i) {
      const int j = static_cast<int>(i) + lag;
      if (j < 0 || j >= static_cast<int>(sampleCount)) {
        continue;
      }
      sum += static_cast<long double>(left[i]) * static_cast<long double>(right[j]);
    }
    const long double normalized = sum / std::sqrt(energyLeft * energyRight);
    if (std::fabs(normalized) > std::fabs(bestCorrelation)) {
      bestCorrelation = normalized;
      bestLag = lag;
    }
  }

  result.bestLagSamples = bestLag;
  result.confidence = static_cast<float>(clamp_unit(static_cast<float>(std::fabs(bestCorrelation))));

  // --- Перевод лага в угол ---
  const float timeDelay = static_cast<float>(bestLag) / static_cast<float>(sampleRate);
  const float ratio = clamp_unit((timeDelay * soundSpeed) / microphoneSpacingMeters);
  constexpr float PI_F = 3.14159265358979323846f;
  result.angleDeg = std::asin(ratio) * 180.0f / PI_F;

  return result;
}

float compute_rms(const std::vector<int16_t>& channel) {
  if (channel.empty()) {
    return 0.0f;
  }
  long double energy = 0.0;
  for (int16_t sample : channel) {
    energy += static_cast<long double>(sample) * static_cast<long double>(sample);
  }
  const long double mean = energy / static_cast<long double>(channel.size());
  const float rms = static_cast<float>(std::sqrt(mean));
  // Нормируем к диапазону int16_t.
  return rms / 32768.0f;
}

} // namespace Audio
