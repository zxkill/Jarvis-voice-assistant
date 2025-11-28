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
                                          float volume,
                                          uint16_t bitsPerSample = 16) {
  const size_t pcmSamples = static_cast<size_t>(frameSamples) * channels;

  std::vector<uint8_t> pcmPayload;
  pcmPayload.reserve(pcmSamples * (bitsPerSample / 8));

  if (bitsPerSample == 8) {
    for (size_t i = 0; i < pcmSamples; ++i) {
      // Формируем тренд от низких значений к высоким, чтобы можно было проверить смещение по знаку.
      const int v = static_cast<int>(i % 200) - 100; // -100..99
      const uint8_t u8 = static_cast<uint8_t>(v + 128); // Беззнаковый PCM8.
      pcmPayload.push_back(u8);
    }
  } else {
    std::vector<int16_t> pcm16(pcmSamples);
    for (size_t i = 0; i < pcmSamples; ++i) {
      pcm16[i] = static_cast<int16_t>((static_cast<int>(i) * 300) - 1000);
    }
    const uint8_t* ptr = reinterpret_cast<const uint8_t*>(pcm16.data());
    pcmPayload.insert(pcmPayload.end(), ptr, ptr + pcm16.size() * sizeof(int16_t));
  }

  std::vector<uint8_t> frame(HEADER_SIZE + pcmPayload.size(), 0);
  frame[0] = 'A';
  frame[1] = 'P';
  frame[2] = 1; // версия
  frame[3] = 0; // флаги
  write_u32(&frame[4], sequence);
  write_u32(&frame[8], timestamp);
  write_u32(&frame[12], sampleRate);
  write_u16(&frame[16], channels);
  write_u16(&frame[18], bitsPerSample); // bitsPerSample
  write_u32(&frame[20], frameSamples);
  write_u32(&frame[24], static_cast<uint32_t>(pcmPayload.size()));
  write_f32(&frame[28], volume);
  write_f32(&frame[32], 0.0f); // reserved
  std::memcpy(frame.data() + HEADER_SIZE, pcmPayload.data(), pcmPayload.size());
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

void test_decode_accepts_pcm8_and_converts_to_signed() {
  // Подготавливаем кадр с восьмибитным PCM: ожидаем, что при декодировании он
  // будет автоматически смещён к int16_t (подобно чтению WAV из примера).
  const auto raw = build_playback_frame(11, 777, 44100, 2, 4, 1.0f, 8);
  AudioPlayback::Frame frame{};
  std::string error;
  assert(AudioPlayback::decode_server_frame(raw.data(), raw.size(), frame, error));
  assert(frame.bitsPerSample == 8);
  // Должно быть 8 сэмплов (4 фрейма * 2 канала), приведённых к int16_t.
  assert(frame.samples.size() == 8);
  // Первый байт (-100 unsigned => -22848 после смещения 0x80 и умножения на 256).
  assert(frame.samples[0] == static_cast<int16_t>((-100) * 256));
  // Байты возле середины должны давать значения около нуля.
  assert(frame.samples[2] == 0);
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
  test_decode_accepts_pcm8_and_converts_to_signed();
  test_decode_rejects_too_many_channels();
  test_handle_frame_updates_stats();
  test_handle_requires_init();
  return 0;
}

