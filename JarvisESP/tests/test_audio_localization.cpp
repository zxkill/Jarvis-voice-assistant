#include "audio_localization.h"

#include <cassert>
#include <cmath>
#include <iostream>
#include <vector>

namespace {

constexpr float PI_F = 3.14159265358979323846f;

std::vector<int16_t> generate_sine(float frequency, float sampleRate, size_t samples, float phase = 0.0f) {
  std::vector<int16_t> out(samples, 0);
  const float amplitude = 30000.0f; // чуть ниже полного диапазона, чтобы не было клиппинга
  for (size_t i = 0; i < samples; ++i) {
    const float t = static_cast<float>(i) / sampleRate;
    const float value = std::sin(2.0f * PI_F * frequency * t + phase);
    out[i] = static_cast<int16_t>(std::round(value * amplitude));
  }
  return out;
}

std::vector<int16_t> apply_delay(const std::vector<int16_t>& src, int lagSamples) {
  std::vector<int16_t> dst(src.size(), 0);
  for (size_t i = 0; i < src.size(); ++i) {
    const int srcIndex = static_cast<int>(i) - lagSamples;
    if (srcIndex >= 0 && srcIndex < static_cast<int>(src.size())) {
      dst[i] = src[static_cast<size_t>(srcIndex)];
    }
  }
  return dst;
}

void test_zero_delay() {
  const float sampleRate = 44100.0f;
  const float micSpacing = 0.15f;
  const size_t samples = 512;
  const auto base = generate_sine(1200.0f, sampleRate, samples);
  const auto left = base;
  const auto right = base;

  const auto estimate = Audio::estimate_direction(left, right, static_cast<uint32_t>(sampleRate), micSpacing);
  assert(std::fabs(estimate.angleDeg) < 1.0f);
  assert(estimate.confidence > 0.9f);
  assert(estimate.bestLagSamples == 0);
}

void test_positive_delay_left_source() {
  const float sampleRate = 44100.0f;
  const float micSpacing = 0.15f;
  const size_t samples = 512;
  const auto base = generate_sine(800.0f, sampleRate, samples);
  const auto left = base;
  const auto right = apply_delay(base, 2); // сигнал доходит до правого микрофона позже

  const auto estimate = Audio::estimate_direction(left, right, static_cast<uint32_t>(sampleRate), micSpacing);
  assert(estimate.bestLagSamples > 0);
  assert(estimate.angleDeg > 0.0f);
  assert(estimate.angleDeg < 90.0f);
  assert(estimate.confidence > 0.6f);
}

void test_negative_delay_right_source() {
  const float sampleRate = 44100.0f;
  const float micSpacing = 0.15f;
  const size_t samples = 512;
  const auto base = generate_sine(1000.0f, sampleRate, samples);
  const auto left = apply_delay(base, 2);
  const auto right = base;

  const auto estimate = Audio::estimate_direction(left, right, static_cast<uint32_t>(sampleRate), micSpacing);
  assert(estimate.bestLagSamples < 0);
  assert(estimate.angleDeg < 0.0f);
  assert(estimate.angleDeg > -90.0f);
  assert(estimate.confidence > 0.6f);
}

void test_rms_normalization() {
  const float sampleRate = 44100.0f;
  const auto base = generate_sine(500.0f, sampleRate, 512);
  const float rms = Audio::compute_rms(base);
  const float expected = 30000.0f / std::sqrt(2.0f) / 32768.0f;
  assert(std::fabs(rms - expected) < 0.02f);
}

} // namespace

int main() {
  test_zero_delay();
  test_positive_delay_left_source();
  test_negative_delay_right_source();
  test_rms_normalization();

  std::cout << "Audio localization tests passed" << std::endl;
  return 0;
}
