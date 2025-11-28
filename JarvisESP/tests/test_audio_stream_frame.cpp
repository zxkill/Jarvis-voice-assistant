#include "remote_control.h"
#include "audio_capture.h"

#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>

namespace {

uint32_t read_u32(const std::vector<uint8_t>& data, size_t offset) {
  uint32_t value = 0;
  std::memcpy(&value, data.data() + offset, sizeof(value));
  return value;
}

uint16_t read_u16(const std::vector<uint8_t>& data, size_t offset) {
  uint16_t value = 0;
  std::memcpy(&value, data.data() + offset, sizeof(value));
  return value;
}

uint64_t read_u64(const std::vector<uint8_t>& data, size_t offset) {
  uint64_t value = 0;
  std::memcpy(&value, data.data() + offset, sizeof(value));
  return value;
}

float read_f32(const std::vector<uint8_t>& data, size_t offset) {
  float value = 0.0f;
  std::memcpy(&value, data.data() + offset, sizeof(value));
  return value;
}

void test_binary_frame_encoding() {
  Audio::PcmChunk chunk{};
  chunk.sampleRate = 16000;
  chunk.channels = 2;
  chunk.timestampUs = 987654321ULL;
  chunk.interleaved = {100, -100, 200, -200};

  Audio::Diagnostics diag{};
  diag.localizationEnabled = true;
  diag.frameSamples = 2;
  diag.rmsLeft = 0.123f;
  diag.rmsRight = 0.456f;
  diag.microphoneSpacingMeters = 0.15f;
  diag.directionDeg = 33.0f;
  diag.confidence = 0.78f;

  const uint32_t sequence = 42;
  RemoteControl::AudioStreamConfig cfg{};
  cfg.xiaoZhiCompat = false; // тестируем внутренний формат AF
  const auto frame = RemoteControl::build_audio_stream_frame(cfg, chunk, diag, sequence, 60, 16000, 2);

  const size_t pcmBytes = chunk.interleaved.size() * sizeof(int16_t);
  const size_t expectedHeader = 52;
  assert(frame.size() == expectedHeader + pcmBytes);
  assert(frame[0] == 'A');
  assert(frame[1] == 'F');
  assert(frame[2] == 1);
  assert((frame[3] & 0x01) == 0x01); // локализация активна

  assert(read_u32(frame, 4) == sequence);
  assert(read_u64(frame, 8) == chunk.timestampUs);
  assert(read_u32(frame, 16) == chunk.sampleRate);
  assert(read_u32(frame, 20) == diag.frameSamples);
  assert(read_u16(frame, 24) == chunk.channels);
  assert(read_u16(frame, 26) == 16);
  assert(read_u32(frame, 28) == pcmBytes);

  const float rmsLeft = read_f32(frame, 32);
  const float rmsRight = read_f32(frame, 36);
  const float spacing = read_f32(frame, 40);
  const float direction = read_f32(frame, 44);
  const float confidence = read_f32(frame, 48);

  assert(rmsLeft > 0.12f && rmsLeft < 0.13f);
  assert(rmsRight > 0.45f && rmsRight < 0.46f);
  assert(spacing > 0.14f && spacing < 0.16f);
  assert(direction > 32.5f && direction < 33.5f);
  assert(confidence > 0.77f && confidence < 0.79f);

  // Проверяем, что PCM-данные попали в конец буфера без изменений.
  const int16_t* pcmPtr = reinterpret_cast<const int16_t*>(frame.data() + expectedHeader);
  assert(pcmPtr[0] == chunk.interleaved[0]);
  assert(pcmPtr[1] == chunk.interleaved[1]);
  assert(pcmPtr[2] == chunk.interleaved[2]);
  assert(pcmPtr[3] == chunk.interleaved[3]);
}

} // namespace

int main() {
  test_binary_frame_encoding();
  std::cout << "Audio stream frame encoding test passed" << std::endl;
  return 0;
}
