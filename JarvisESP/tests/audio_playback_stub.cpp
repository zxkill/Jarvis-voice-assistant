#ifndef ARDUINO
#include "audio_playback.h"

namespace AudioPlayback {

// Простейшие заглушки для запуска настольных тестов: они не трогают железо, но позволяют
// проверить упаковку кадров и совместимость протокола без прошивки на ESP32.
bool handle_server_frame(const uint8_t*, size_t) { return true; }
bool init(const Config&) { return true; }
void shutdown() {}
Stats stats() { return Stats{}; }
void reset_stats() {}

} // namespace AudioPlayback
#endif

