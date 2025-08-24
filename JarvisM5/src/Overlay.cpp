#include "Overlay.h"

// Показ текстовой подсказки на заданное время
void Overlay::setText(String t)
{
  text_ = std::move(t);
  textVisible_ = true;
  lastTextMs_  = millis();
}

// скрываем выведенный текст по таймауту
void Overlay::tick()
{
  if (textVisible_ && millis() - lastTextMs_ > TEXT_DISPLAY_TIMEOUT)
    textVisible_ = false;
}

// рисуем всё поверх кадра face-анимации
void Overlay::draw(LGFX_Sprite& gfx)
{
  gfx.startWrite();

  // ───── 1.  ВЕРХНЯЯ ПАНЕЛЬ: время, погода, батарея, иконки ─────
  if (time_.length()) {
    gfx.setFont(&fonts::lgfxJapanGothic_16);
    gfx.drawString(time_, TIME_R.x, TIME_R.y);
  }

  if (weather_.length()) {
    gfx.setFont(&fonts::lgfxJapanGothic_16);
    gfx.drawString(weather_, WEATHER_R.x, WEATHER_R.y);
  }

  // Батарея
  power::drawGauge(gfx, BATTERY_R.x, BATTERY_R.y + 3);

  // Значки Wi-Fi / WebSocket
  {
    int ix = ICONS_R.x;
    int iy = ICONS_R.y + ICONS_R.h / 2;

    // Wi-Fi
    if (wifiConnected_) {
      for (int r = 2; r <= 6; r += 2) gfx.drawCircle(ix + r, iy, r);
      gfx.fillCircle(ix + 8, iy, 2);
    } else {
      gfx.drawLine(ix + 2, iy - 4, ix + 10, iy + 4);
      gfx.drawLine(ix + 2, iy + 4, ix + 10, iy - 4);
    }

    // WebSocket
    int sx = ix + 20;
    if (wsConnected_) {
      gfx.fillCircle(sx,     iy, 2);
      gfx.fillCircle(sx + 8, iy, 2);
      gfx.drawLine(  sx + 2, iy, sx + 6, iy);
    } else {
      gfx.drawCircle(sx,     iy, 2);
      gfx.drawCircle(sx + 8, iy, 2);
    }
  }

  // ───── 1-A.  ГОРИЗОНТАЛЬНАЯ ЛИНИЯ-РАЗДЕЛИТЕЛЬ ─────
  gfx.drawFastHLine(0, TIME_R.y + TIME_R.h, 320, COL_FOREGROUND);

  // ───── 2.  ОСНОВНОЙ ТЕКСТ ─────
  if (textVisible_ && text_.length()) {
    gfx.setFont(&fonts::lgfxJapanGothic_16);
    int16_t y = MAIN_R.y;
    const int lineH = 18;

    String word, line;
    auto flushLine = [&](bool force) {
      if (force && line.length()) {
        gfx.drawString(line, MAIN_R.x, y);
        y += lineH;
        line = "";
      }
    };

    for (uint16_t i = 0; i <= text_.length(); ++i) {
      char c = i < text_.length() ? text_[i] : ' ';
      if (c == ' ' || c == '\n') {
        if (word.length()) {
          String cand = line.length() ? line + ' ' + word : word;
          if (gfx.textWidth(cand) <= MAIN_R.w) line = cand;
          else { flushLine(true); line = word; }
          word = "";
        }
        if (c == '\n') flushLine(true);
      } else {
        word += c;
      }
    }
    flushLine(true);
  }

  gfx.endWrite();
}
