#include "audio_playback.h"

#include <cassert>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr size_t HEADER_SIZE = 36; // Должен совпадать с реализацией приёмника.

void write_u16(uint8_t* dst, uint16_t value) {
  dst[0] = static_cast<uint8_t>(value & 0xFFu);
  dst[1] = static_cast<uint8_t>((value >> 8) & 0xFFu);
}

void write_u32(uint8_t* dst, uint32_t value) {
  for (int i = 0; i < 4; ++i) {
    dst[i] = static_cast<uint8_t>((value >> (i * 8)) & 0xFFu);
  }
}

void write_f32(uint8_t* dst, float value) {
  std::memcpy(dst, &value, sizeof(float));
}

std::vector<uint8_t> build_playback_frame(uint32_t sequence,
                                          uint32_t timestamp,
                                          uint32_t sampleRate,
                                          uint16_t channels,
                                          uint32_t frameSamples,
                                          float volume) {
  const size_t pcmSamples = static_cast<size_t>(frameSamples) * channels;
  std::vector<int16_t> pcm(pcmSamples);
  for (size_t i = 0; i < pcmSamples; ++i) {
    pcm[i] = static_cast<int16_t>((static_cast<int>(i) * 300) - 1000);
  }

  std::vector<uint8_t> frame(HEADER_SIZE + pcmSamples * sizeof(int16_t), 0);
  frame[0] = 'A';
  frame[1] = 'P';
  frame[2] = 1; // версия
  frame[3] = 0; // флаги
  write_u32(&frame[4], sequence);
  write_u32(&frame[8], timestamp);
  write_u32(&frame[12], sampleRate);
  write_u16(&frame[16], channels);
  write_u16(&frame[18], 16); // bitsPerSample
  write_u32(&frame[20], frameSamples);
  write_u32(&frame[24], static_cast<uint32_t>(pcmSamples * sizeof(int16_t)));
  write_f32(&frame[28], volume);
  write_f32(&frame[32], 0.0f); // reserved
  std::memcpy(frame.data() + HEADER_SIZE, pcm.data(), pcmSamples * sizeof(int16_t));
  return frame;
}

void test_decode_server_frame_success() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  assert(AudioPlayback::init(cfg));

  const auto raw = build_playback_frame(5, 123456, 22050, 2, 4, 0.75f);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));
  assert(frame.sequence == 5);
  assert(frame.timestampUs == 123456);
  assert(frame.sampleRate == 22050);
  assert(frame.channels == 2);
  assert(frame.bitsPerSample == 16);
  assert(frame.samples.size() == 8);
  assert(frame.volume > 0.74f && frame.volume < 0.76f);
  AudioPlayback::shutdown();
}

void test_init_primes_silence() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  cfg.frameSamplesHint = 256;
  assert(AudioPlayback::init(cfg));
  const auto stats = AudioPlayback::stats();
  assert(stats.initialized);
  assert(stats.silencePrimed == 1);
  assert(!stats.muted);
  assert(stats.idleTransitions == 0);
  AudioPlayback::shutdown();
}

void test_external_i2s_mode_primes_silence() {
  AudioPlayback::Config cfg{};
  cfg.mode = AudioPlayback::OutputMode::ExternalI2S;
  cfg.defaultSampleRate = 44100;
  cfg.frameSamplesHint = 512;
  assert(AudioPlayback::init(cfg));
  const auto stats = AudioPlayback::stats();
  // Внешний усилитель тоже должен получать тишину в DMA и оставаться готовым к первому кадру TTS.
  assert(stats.initialized);
  assert(stats.silencePrimed == 1);
  assert(!stats.muted);
  AudioPlayback::shutdown();
}

void test_decode_server_frame_rejects_magic() {
  const auto raw = build_playback_frame(1, 0, 16000, 1, 2, 1.0f);
  std::vector<uint8_t> broken = raw;
  broken[0] = 'X';
  AudioPlayback::Frame frame{};
  std::string error;
  assert(!AudioPlayback::decode_server_frame(broken.data(), broken.size(), frame, error));
  assert(error == "bad-magic");
}

void test_decode_rejects_too_many_channels() {
  auto raw = build_playback_frame(3, 0, 16000, 1, 2, 1.0f);
  // Портим поле channels на заведомо неподдерживаемое значение, чтобы проверить отказоустойчивость приёмника.
  raw[16] = 3; // channels low byte
  raw[17] = 0; // channels high byte
  AudioPlayback::Frame frame{};
  std::string error;
  assert(!AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));
  assert(error == "bad-channels");
}

void test_handle_frame_updates_stats() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  cfg.queueCapacity = 3;
  assert(AudioPlayback::init(cfg));
  AudioPlayback::reset_stats();

  const auto raw = build_playback_frame(7, 42, 16000, 2, 8, 1.0f);
  assert(AudioPlayback::handle_server_frame(raw.data(), raw.size()));
  const auto stats = AudioPlayback::stats();
  assert(stats.framesAccepted == 1);
  assert(stats.queueDepth == 1);
  assert(stats.lastSequence == 7);
  assert(stats.lastSampleRate == 16000);
  assert(stats.lastVolume > 0.99f && stats.lastVolume < 1.01f);
  AudioPlayback::shutdown();
}

void test_handle_requires_init() {
  AudioPlayback::shutdown();
  AudioPlayback::reset_stats();
  const auto raw = build_playback_frame(1, 0, 16000, 1, 2, 1.0f);
  assert(!AudioPlayback::handle_server_frame(raw.data(), raw.size()));
  const auto stats = AudioPlayback::stats();
  assert(stats.framesRejected == 1);
  assert(stats.lastError == "not-initialized");
}

} // namespace

int main() {
  test_decode_server_frame_success();
  test_init_primes_silence();
  test_external_i2s_mode_primes_silence();
  test_decode_server_frame_rejects_magic();
  test_decode_rejects_too_many_channels();
  test_handle_frame_updates_stats();
  test_handle_requires_init();
  return 0;
}

