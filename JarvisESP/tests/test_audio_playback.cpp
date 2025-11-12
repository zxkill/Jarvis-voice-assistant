#include "audio_playback.h"

#include <algorithm>
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

std::vector<uint8_t> build_playback_frame_with_pcm(uint32_t sequence,
                                                   uint32_t timestamp,
                                                   uint32_t sampleRate,
                                                   uint16_t channels,
                                                   uint32_t frameSamples,
                                                   float volume,
                                                   const std::vector<int16_t>& pcm) {
  const size_t pcmSamples = static_cast<size_t>(frameSamples) * channels;
  assert(pcm.size() == pcmSamples);

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
  return build_playback_frame_with_pcm(sequence, timestamp, sampleRate, channels, frameSamples, volume, pcm);
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

void test_decode_server_frame_rejects_magic() {
  const auto raw = build_playback_frame(1, 0, 16000, 1, 2, 1.0f);
  std::vector<uint8_t> broken = raw;
  broken[0] = 'X';
  AudioPlayback::Frame frame{};
  std::string error;
  assert(!AudioPlayback::decode_server_frame(broken.data(), broken.size(), frame, error));
  assert(error == "bad-magic");
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

void test_convert_silence_matches_reference() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  assert(AudioPlayback::init(cfg));

  const std::vector<int16_t> pcm(8, 0);
  const auto raw = build_playback_frame_with_pcm(11, 0, 16000, 1, 8, 1.0f, pcm);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));
  const auto words = AudioPlayback::detail::convert_pcm_to_dac_words(frame,
                                                                     cfg.defaultVolume,
                                                                     cfg.dacOutput);
  assert(words.size() == pcm.size() * 2u);
  for (size_t i = 0; i < pcm.size(); ++i) {
    assert(words[i * 2u] == 0x8000u);     // левый канал
    assert(words[i * 2u + 1u] == 0x8000u); // правый канал (тишина)
  }

  AudioPlayback::shutdown();
}

void test_convert_extremes_and_volume() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  assert(AudioPlayback::init(cfg));

  // Проверяем насыщение при штатной громкости.
  const std::vector<int16_t> pcmSaturated = {32767, -32768};
  const auto rawSaturated = build_playback_frame_with_pcm(12, 0, 16000, 1, 2, 1.0f, pcmSaturated);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(rawSaturated.data(), rawSaturated.size(), frame, error));
  auto words = AudioPlayback::detail::convert_pcm_to_dac_words(frame,
                                                               cfg.defaultVolume,
                                                               cfg.dacOutput);
  assert(words.size() == pcmSaturated.size() * 2u);
  assert(words[0] == 0xFF00u);
  assert(words[1] == 0x8000u);
  assert(words[2] == 0x0000u);
  assert(words[3] == 0x8000u);

  // А теперь проверяем уменьшение амплитуды при громкости 0.5.
  const std::vector<int16_t> pcmHalf = {16384, -16384};
  const auto rawHalf = build_playback_frame_with_pcm(13, 0, 16000, 1, 2, 0.5f, pcmHalf);
  AudioPlayback::Frame frameHalf{};
  assert(AudioPlayback::decode_server_frame(rawHalf.data(), rawHalf.size(), frameHalf, error));
  words = AudioPlayback::detail::convert_pcm_to_dac_words(frameHalf,
                                                          cfg.defaultVolume,
                                                          cfg.dacOutput);
  assert(words.size() == pcmHalf.size() * 2u);
  assert(words[0] == 0xA000u);
  assert(words[1] == 0x8000u);
  assert(words[2] == 0x6000u);
  assert(words[3] == 0x8000u);

  AudioPlayback::shutdown();
}

void test_convert_stereo_downmix_rounding() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  assert(AudioPlayback::init(cfg));

  const std::vector<int16_t> pcm = {32767, -32768};
  const auto raw = build_playback_frame_with_pcm(13, 0, 16000, 2, 1, 1.0f, pcm);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));
  const auto words = AudioPlayback::detail::convert_pcm_to_dac_words(frame,
                                                                     cfg.defaultVolume,
                                                                     cfg.dacOutput);
  assert(words.size() == 2u);
  // Усреднение (32767 + -32768) / 2 = -1 -> 0x7F00 после смещения (левый канал).
  assert(words[0] == 0x7F00u);
  assert(words[1] == 0x8000u);

  AudioPlayback::shutdown();
}

void test_convert_matches_reference_sketch() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  assert(AudioPlayback::init(cfg));

  const std::vector<int16_t> pcm = {0, 1024, -1024, 8192, -8192};
  const auto raw = build_playback_frame_with_pcm(21, 0, 16000, 1, pcm.size(), 1.0f, pcm);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));
  const auto words = AudioPlayback::detail::convert_pcm_to_dac_words(frame,
                                                                     cfg.defaultVolume,
                                                                     cfg.dacOutput);
  assert(words.size() == pcm.size() * 2u);

  for (size_t i = 0; i < pcm.size(); ++i) {
    const int32_t shifted = static_cast<int32_t>(pcm[i]) + 32768;
    const uint8_t expectedByte = static_cast<uint8_t>(std::clamp<int32_t>(shifted >> 8, 0, 255));
    const uint16_t expectedWord = static_cast<uint16_t>(static_cast<uint16_t>(expectedByte) << 8);
    assert(words[i * 2u] == expectedWord);
    assert(words[i * 2u + 1u] == 0x8000u);
  }

  AudioPlayback::shutdown();
}

void test_convert_mirror_layout() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  cfg.dacOutput = AudioPlayback::DacOutput::MirrorBoth;
  assert(AudioPlayback::init(cfg));

  const std::vector<int16_t> pcm = {0, 2048, -2048};
  const auto raw = build_playback_frame_with_pcm(30, 0, 16000, 1, pcm.size(), 1.0f, pcm);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));

  const auto words = AudioPlayback::detail::convert_pcm_to_dac_words(frame,
                                                                     cfg.defaultVolume,
                                                                     cfg.dacOutput);
  assert(words.size() == pcm.size() * 2u);
  for (size_t i = 0; i < pcm.size(); ++i) {
    const int32_t shifted = static_cast<int32_t>(pcm[i]) + 32768;
    const uint8_t expectedByte = static_cast<uint8_t>(std::clamp<int32_t>(shifted >> 8, 0, 255));
    const uint16_t expectedWord = static_cast<uint16_t>(static_cast<uint16_t>(expectedByte) << 8);
    assert(words[i * 2u] == expectedWord);
    assert(words[i * 2u + 1u] == expectedWord);
  }

  AudioPlayback::shutdown();
}

void test_convert_right_only_layout() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  cfg.dacOutput = AudioPlayback::DacOutput::RightOnly;
  assert(AudioPlayback::init(cfg));

  const std::vector<int16_t> pcm = {0, 4096, -4096};
  const auto raw = build_playback_frame_with_pcm(31, 0, 16000, 1, pcm.size(), 1.0f, pcm);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));

  const auto words = AudioPlayback::detail::convert_pcm_to_dac_words(frame,
                                                                     cfg.defaultVolume,
                                                                     cfg.dacOutput);
  assert(words.size() == pcm.size() * 2u);
  for (size_t i = 0; i < pcm.size(); ++i) {
    const int32_t shifted = static_cast<int32_t>(pcm[i]) + 32768;
    const uint8_t expectedByte = static_cast<uint8_t>(std::clamp<int32_t>(shifted >> 8, 0, 255));
    const uint16_t expectedWord = static_cast<uint16_t>(static_cast<uint16_t>(expectedByte) << 8);
    assert(words[i * 2u] == 0x8000u);            // левый канал глушим тишиной
    assert(words[i * 2u + 1u] == expectedWord);  // правый несёт полезный сигнал
  }

  AudioPlayback::shutdown();
}

} // namespace

int main() {
  test_decode_server_frame_success();
  test_init_primes_silence();
  test_decode_server_frame_rejects_magic();
  test_handle_frame_updates_stats();
  test_handle_requires_init();
  test_convert_silence_matches_reference();
  test_convert_extremes_and_volume();
  test_convert_stereo_downmix_rounding();
  test_convert_matches_reference_sketch();
  test_convert_mirror_layout();
  test_convert_right_only_layout();
  return 0;
}

