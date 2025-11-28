#include "audio_capture.h"

#include <cassert>
#include <iostream>

int main() {
  // --- Проверяем инициализацию без локальной локализации ---
  Audio::Config cfg{};
  cfg.sampleRate = 44100;
  cfg.frameSamples = 256;
  cfg.microphoneSpacingMeters = 0.15f;
  cfg.enableLocalization = false;

  assert(Audio::init(cfg));
  Audio::Diagnostics diag = Audio::latest_diagnostics();
  assert(!diag.localizationEnabled);
  assert(diag.sampleRate == cfg.sampleRate);
  assert(diag.frameSamples == cfg.frameSamples);
  assert(diag.microphoneSpacingMeters == cfg.microphoneSpacingMeters);
  assert(!diag.streamHasChunk);
  Audio::shutdown();

  // --- Проверяем, что включение локализации отражается в диагностике ---
  cfg.enableLocalization = true;
  cfg.frameSamples = 128;
  assert(Audio::init(cfg));
  diag = Audio::latest_diagnostics();
  assert(diag.localizationEnabled);
  assert(diag.frameSamples == cfg.frameSamples);
  assert(diag.microphoneSpacingMeters == cfg.microphoneSpacingMeters);
  Audio::shutdown();

  std::cout << "Audio capture config tests passed" << std::endl;
  return 0;
}
