#include "audio_playback.h"

#include <cassert>
#include <cstdint>
#include <vector>
#include <cstring>

namespace {

std::vector<uint8_t> build_pcm_bytes(size_t samples) {
  std::vector<uint8_t> out(samples * sizeof(int16_t));
  for (size_t i = 0; i < samples; ++i) {
    const int16_t value = static_cast<int16_t>((i * 200) - 500);
    std::memcpy(out.data() + i * sizeof(int16_t), &value, sizeof(int16_t));
  }
  return out;
}

void test_stream_lifecycle() {
  AudioPlayback::Config cfg{};
  cfg.defaultSampleRate = 16000;
  cfg.frameSamplesHint = 256;
  assert(AudioPlayback::init(cfg));

  // После инициализации буфер должен быть прогрет тишиной даже в хостовой сборке.
  auto stats = AudioPlayback::stats();
  assert(stats.initialized);
  assert(stats.silencePrimed >= 1);

  // Без audio_start приём должен отклонять входящие данные.
  AudioPlayback::reset_stats();
  auto pcm = build_pcm_bytes(4);
  assert(!AudioPlayback::feed_stream_chunk(pcm.data(), pcm.size()));
  stats = AudioPlayback::stats();
  assert(stats.chunksRejected == 1);
  assert(stats.lastError == "stream-inactive");

  // Теперь стартуем поток и подаём два чанка подряд.
  assert(AudioPlayback::start_stream(16000, 1, 1.0f));
  pcm = build_pcm_bytes(8);
  assert(AudioPlayback::feed_stream_chunk(pcm.data(), pcm.size()));
  assert(AudioPlayback::feed_stream_chunk(pcm.data(), pcm.size()));

  stats = AudioPlayback::stats();
  assert(stats.chunksAccepted == 2);
  assert(stats.lastSequence == 2);
  assert(stats.lastSampleRate == 16000);
  assert(stats.lastVolume > 0.99f && stats.lastVolume < 1.01f);

  AudioPlayback::stop_stream("test-end");
  stats = AudioPlayback::stats();
  assert(stats.queueDepth == 0);
}

void test_rejects_without_init() {
  AudioPlayback::shutdown();
  AudioPlayback::reset_stats();
  const auto pcm = build_pcm_bytes(2);
  assert(!AudioPlayback::feed_stream_chunk(pcm.data(), pcm.size()));
  const auto stats = AudioPlayback::stats();
  assert(stats.chunksRejected == 1);
  assert(stats.lastError == "not-initialized");
}

} // namespace

int main() {
  test_stream_lifecycle();
  test_rejects_without_init();
  return 0;
}

