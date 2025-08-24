#pragma once
#include <M5Unified.h>
#include <vector>
#include <cstdarg>
#include "DisplayAdapter.h"
#include "Config.h"

enum class LogLevel { INFO, WARN, ERROR, DEBUG };

class Logger {
public:
  // ── init ────────────────────────────────────────────────────────
  static void init() {
    auto& L = instance();
    L.lines.clear();

    if (!L.created) {
      L.spr.setColorDepth(16);
      L.spr.createSprite(LOG_REGION.w, LOG_REGION.h);
      L.created = true;
    }
    L.screen   = true;
    L.autoPush = true;              // в начале = ON
    L.redraw();
    present();                      // показать чистую область
  }

  // ── основной log(fmt, ...) ──────────────────────────────────────
  static void log(LogLevel lvl, const char* fmt, ...) {
    char buf[128];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    const char* p = lvl == LogLevel::ERROR ? "[E] " :
                    lvl == LogLevel::WARN  ? "[W] " : "[I] ";
    Serial.print(p); Serial.println(buf);

    auto& L = instance();
    if (!L.screen) return;

    L.lines.push_back(String(p) + buf);
    if (L.lines.size() > 128) L.lines.erase(L.lines.begin());

    L.redraw();
    if (L.autoPush) present();      // только если разрешено
  }
  static void log(LogLevel lvl, const String& s) { log(lvl, "%s", s.c_str()); }

  // ── управляем режимом ───────────────────────────────────────────
  static void enableScreenLogging(bool en) { instance().screen = en; }
  static void enableAutoPresent(bool en)   { instance().autoPush = en; }

  // ── показ лог-спрайта на дисплее ────────────────────────────────
  static void present() {
    auto& L = instance();
    if (L.created) L.spr.pushSprite(LOG_REGION.x, LOG_REGION.y);
  }

  // ── встроить в существующий спрайт frame ───────────────────────
  static void renderTo(LGFX_Sprite& dst) { instance().blit(dst); }

private:
  Logger() = default;

  void redraw() {
    spr.fillRect(0, 0, LOG_REGION.w, LOG_REGION.h, COL_BACKGROUND);
    spr.setTextSize(1);               // встроенный 6×8
    spr.setTextColor(COL_FOREGROUND);

    const int lineH   = 8;
    const int fit     = LOG_REGION.h / lineH;     // обычно 5
    const int total   = lines.size();
    const int start   = total > fit ? total - fit : 0;

    for (int i = 0; i < fit && start + i < total; ++i) {
      spr.setCursor(1, i * lineH);
      spr.print(lines[start + i]);
    }
  }

  void blit(LGFX_Sprite& dst) {
    spr.pushSprite(&dst, LOG_REGION.x, LOG_REGION.y);
  }

  static Logger& instance() {
    static Logger L;
    return L;
  }

  std::vector<String> lines;
  bool  screen   {true};
  bool  autoPush {true};

  LGFX_Sprite spr{&M5.Display};
  bool        created{false};
};
